"""Plumbing shared by the insured-fleet routers (Ref / Fleet / Leasing / Policies / History).

Everything here is domain logic that several routers need and none owns: normalising names the way
the STORED generated columns do, finding-or-creating reference rows, telling the audit trigger who
is making the change, turning a database constraint into the right HTTP status, and rendering an
audit snapshot pair as the field-level diff a UI can show.

Two conventions every router in this domain follows:

  * **a grid returns `{"items": [...], "total": N}`** — never a bare array, so the client can page
    without a second call to learn the size;
  * **sorting is whitelist-driven** — `sort=<field>&order=asc|desc`, the field looked up in a map
    the router declares, so no caller string ever reaches ORDER BY.
"""
import re
from datetime import date
from decimal import Decimal
from typing import Any, Iterable, Optional, Sequence

from fastapi import status
from sqlalchemy import select, text, desc
from sqlalchemy.exc import IntegrityError

from Database import ApiToken
from Database.RefModels import Airline, Party, PartyContact
from Database.FleetModels import (
    Aircraft, AircraftType, AircraftEngine, EngineType, ServiceInfo, AircraftCategory,
    TEMPLATE_VIEWS,
)
from Database.LeasingModels import Agreement, AircraftLease
from Database.PolicyModels import Policy, Coverage

DB = "aixii"


# ==============================================================================================
# normalisation — must mirror the STORED generated columns exactly
# ==============================================================================================

def norm(value: str) -> str:
    """upper + trim. Mirrors `ref.party.name_normalized`, `ref.airline` matching and
    `fleet.aircraft_type.master_series_normalized`."""
    return value.strip().upper()


def norm_reg(value: str) -> str:
    """upper + strip separators. Mirrors `fleet.aircraft.registration_normalized`, so 'YLLTD'
    finds 'YL-LTD'."""
    return "".join(ch for ch in value.upper() if ch.isalnum())


# ==============================================================================================
# find-or-create for the reference tables
# ==============================================================================================
# Each matches on the column that carries the UNIQUE constraint, so 'AerCap' and 'AERCAP ' can
# never become two rows. They flush, so the caller gets a usable id without committing.

async def get_or_create_airline(session, name: Optional[str]) -> Optional[Airline]:
    if not name or not name.strip():
        return None
    row = (await session.execute(
        select(Airline).where(Airline.airline_name.ilike(name.strip())).order_by(Airline.id).limit(1)
    )).scalar_one_or_none()
    if row is None:
        row = Airline(airline_name=name.strip())
        session.add(row)
        await session.flush()
    return row


async def get_or_create_party(session, name: Optional[str]) -> Optional[Party]:
    """One table for every role — lessor, insured, reinsured, retrocedent. Which role a party is
    playing is decided by the column that references it, never by anything stored here."""
    if not name or not name.strip():
        return None
    row = (await session.execute(
        select(Party).where(Party.name_normalized == norm(name))
    )).scalar_one_or_none()
    if row is None:
        row = Party(name=name.strip())
        session.add(row)
        await session.flush()
    return row


class AmbiguousType(ValueError):
    """A master series that several manufacturers build, asked for without naming one. Carries the
    message the router returns as a 400, listing the manufacturers so the caller can pick."""


async def _get_or_create_type(session, model, master_series: Optional[str],
                              manufacturer: Optional[str], label: str,
                              category: Optional[str] = None):
    """Find a type row, or create it. Shared by airframes and engines.

    An ENGINE model is keyed by the PAIR of manufacturer and master series. An AIRFRAME type is
    keyed by that pair AND its category, so 'Airbus A300-600' names two rows — the freighter and
    the passenger aircraft — and picking between them is the caller's business, not a coin toss.

    Each name that is left out widens the search, and an ambiguous search is an error rather than
    a guess, because guessing files the aircraft under the wrong builder or the wrong role and
    nothing downstream would notice:

      * everything given          -> match exactly, create if absent;
      * manufacturer omitted      -> match on the rest; several builders -> AmbiguousType;
      * category omitted (airframes) -> match on the rest; several categories -> AmbiguousType;
      * nothing matches           -> create, defaulting the category to `passenger`, which is
                                     what a fleet of insured aircraft is made of.
    """
    if not master_series or not master_series.strip():
        return None
    conds = [model.master_series_normalized == norm(master_series)]
    if manufacturer and manufacturer.strip():
        conds.append(model.manufacturer_normalized == norm(manufacturer))
    has_category = hasattr(model, "category")
    if has_category and category:
        conds.append(model.category == category)

    matches = (await session.execute(
        select(model).where(*conds).order_by(model.id))).scalars().all()
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        if not (manufacturer and manufacturer.strip()):
            builders = sorted({m.manufacturer or "(no manufacturer)" for m in matches})
            if len(builders) > 1:
                raise AmbiguousType(
                    f"{label} '{master_series.strip()}' is built by several manufacturers "
                    f"({', '.join(builders)}). Send `manufacturer` as well to say which.")
        roles = sorted({enum_value(m.category) for m in matches}) if has_category else []
        if len(roles) > 1:
            raise AmbiguousType(
                f"{label} '{master_series.strip()}' exists as {', '.join(roles)}. Send "
                f"`aircraft_category` as well to say which — a freighter and a passenger "
                f"aircraft of the same series are different rows.")
        return matches[0]

    # Nothing matched: create it. Only an exact request can create, because creating from a
    # partial name is how a second 'A320' with no manufacturer appears beside the real one.
    fields = {"master_series": master_series.strip()}
    if manufacturer and manufacturer.strip():
        fields["manufacturer"] = manufacturer.strip()
    if has_category:
        fields["category"] = category or AircraftCategory.PASSENGER
    row = model(**fields)
    session.add(row)
    await session.flush()
    return row


async def get_or_create_aircraft_type(session, master_series: Optional[str],
                                      manufacturer: Optional[str] = None,
                                      category: Optional[str] = None) -> Optional[AircraftType]:
    return await _get_or_create_type(session, AircraftType, master_series, manufacturer,
                                     "Aircraft type", category)


async def get_or_create_engine_type(session, master_series: Optional[str],
                                    manufacturer: Optional[str] = None) -> Optional[EngineType]:
    return await _get_or_create_type(session, EngineType, master_series, manufacturer,
                                     "Engine type")


async def find_aircraft(session, *, registration: Optional[str] = None,
                        msn: Optional[str] = None, options: Sequence = ()) -> Optional[Aircraft]:
    """MSN wins: it is the airframe's identity, and a tail number can be reissued to a different
    aircraft after a re-registration.

    Pass `options` when the caller is about to read the relationships. Without them the model
    defaults apply — `lazy="selectin"`, one extra round trip per relationship, against a remote
    database — and a caller that only needs the row pays nothing for leaving them out.
    """
    if msn and msn.strip():
        row = (await session.execute(
            select(Aircraft).where(Aircraft.msn == msn.strip()).options(*options)
        )).unique().scalar_one_or_none()
        if row is not None:
            return row
    if registration and registration.strip():
        return (await session.execute(
            select(Aircraft)
            .where(Aircraft.registration_normalized == norm_reg(registration))
            .order_by(Aircraft.id).limit(1).options(*options)
        )).unique().scalar_one_or_none()
    return None


async def set_actor(session, token: Optional[ApiToken]) -> None:
    """Tell the audit trigger who is making this change. Transaction-local (the `true` argument),
    so it cannot leak into another request that later borrows the same pooled connection.

    EVERY write path must call this before it writes. Without it `audit.change_log.changed_by`
    records the database login — `svc_api` — which tells nobody anything.
    """
    actor = token.name if token is not None else "service-token"
    await session.execute(text("SELECT set_config('app.actor', :actor, true)"), {"actor": actor})


# ==============================================================================================
# the agreed value, depreciated
# ==============================================================================================

def agreed_value_at(preliminary: Optional[Decimal], ratio: Optional[Decimal],
                    start: Optional[date], fixed: bool, on: date) -> Optional[float]:
    """Mirror of the SQL function `leasing.agreed_value_at(...)` — compounding on WHOLE years
    elapsed since the depreciation start.

    Two implementations exist because reports need it in SQL and the API needs it per row without a
    round trip. They are asserted equal by the domain test; change both or neither.

    `leasing.aircraft_lease.agreed_value_final` holds what the schedule STATED. This is what the
    formula gives. The read layer shows both — it never overwrites the stated figure.
    """
    if preliminary is None:
        return None
    if fixed or ratio is None or start is None or on <= start:
        return float(preliminary)
    years = on.year - start.year - ((on.month, on.day) < (start.month, start.day))
    return round(float(preliminary) * (1 - float(ratio) / 100.0) ** years, 2)


# ==============================================================================================
# grid sorting and paging
# ==============================================================================================

class SortError(ValueError):
    """Unknown or invalid sort request. Its message lists the allowed keys, so a caller does not
    have to guess them from the docs; the router returns it as a 400."""


def apply_sort(stmt, *, sort: Optional[str], order: Optional[str], sortmap: dict, tiebreak):
    """Order `stmt` by a WHITELISTED column.

    NULLs always sink to the bottom whichever direction is asked for — a grid sorted by "agreed
    value, biggest first" should not open on a screen of blanks. `tiebreak` keeps paging stable
    when the sort column ties, which dates and enums do constantly.
    """
    if not sort:
        return stmt.order_by(*tiebreak)
    key = sort.strip().lower()
    if key not in sortmap:
        raise SortError(f"Unknown sort field '{sort}'. Allowed: {', '.join(sorted(sortmap))}")
    direction = (order or "asc").strip().lower()
    if direction not in ("asc", "desc"):
        raise SortError("`order` must be 'asc' or 'desc'")
    column = sortmap[key]
    expression = column.desc() if direction == "desc" else column.asc()
    return stmt.order_by(expression.nulls_last(), *tiebreak)


def typeahead_order(column, q: str):
    """Rank a reference lookup: prefix matches first, then anything else containing the text, then
    alphabetically."""
    return [desc(column.ilike(f"{q}%")), column]


# ==============================================================================================
# turning a database constraint into the right HTTP answer
# ==============================================================================================
# The schema states its invariants once, as constraints. Re-checking them in Python before every
# write would mean two sources of truth and a race between them, so instead the write goes ahead
# and the violation is translated here.

_SQLSTATE_STATUS = {
    "23505": status.HTTP_409_CONFLICT,            # unique_violation
    "23P01": status.HTTP_409_CONFLICT,            # exclusion_violation
    "23503": status.HTTP_409_CONFLICT,            # foreign_key_violation
    "23514": status.HTTP_400_BAD_REQUEST,         # check_violation
}

# constraint name -> what to tell the caller. Anything not listed falls back to a generic message
# built from the sqlstate, so a new constraint is never reported as a 500.
_CONSTRAINT_MESSAGES = {
    "uq_party_name_normalized":
        "A counterparty with that name already exists (names are compared trimmed and upper-cased).",
    "uq_aircraft_type_manufacturer_series":
        "That manufacturer and master series already exist as an aircraft type.",
    "uq_engine_type_manufacturer_series":
        "That manufacturer and master series already exist as an engine type.",
    "uq_aircraft_msn":
        "That MSN already belongs to a different aircraft.",
    "uq_aircraft_engine_installation":
        "That aircraft already has an engine recorded in this position on this date.",
    "ck_aircraft_engine_position":
        "Engine position must be 1..4, numbered left to right as the pilot sees it.",
    "uq_agreement_name_start":
        "A lease agreement with that name and start date already exists.",
    "uq_aircraft_lease_effective":
        "That aircraft already has lease terms under this agreement effective on that date.",
    "ck_aircraft_lease_depreciation_ratio":
        "`depreciation_ratio` is a percent and must be between 0 and 100.",
    "ck_aircraft_lease_amounts_non_negative":
        "Amounts cannot be negative.",
    "uq_policy_insured_period":
        "That insured already has a policy for exactly this period.",
    "ck_policy_period":
        "`period_to` must not be earlier than `period_from`.",
    "ck_policy_reinsured_amount":
        "`reinsured_amount` is a percent and must be between 0 and 100.",
    "ck_policy_amounts_non_negative":
        "Amounts cannot be negative.",
    "ck_policy_currency":
        "Currency must be one of USD, EUR, GBP.",
    "ck_agreement_currency":
        "Currency must be one of USD, EUR, GBP.",
    "ex_coverage_no_overlap":
        "That aircraft is already covered by a policy over part of this period. An aircraft holds "
        "one policy at a time — end the existing coverage or correct it instead of adding a second.",
    "uq_coverage_aircraft_policy":
        "That aircraft is already on this policy from that date.",
    "ck_coverage_period":
        "`covered_to` must not be earlier than `covered_from`.",
}

_GENERIC = {
    "23505": "That record already exists.",
    "23P01": "That record overlaps one that already exists.",
    "23503": "That change would break a link to another record — it is still referenced, or the "
             "record it points at does not exist.",
    "23514": "The values sent fail a rule the schema enforces.",
}


_CONSTRAINT_IN_TEXT = re.compile(r'constraint "([^"]+)"')


def integrity_error(ex: IntegrityError) -> tuple[int, str]:
    """(http status, message) for a constraint violation — the database's own verdict, not a guess.

    WHERE THE CONSTRAINT NAME LIVES. SQLAlchemy's asyncpg adapter re-raises the driver error as its
    own `IntegrityError`, and that wrapper carries only `sqlstate` / `pgcode`. The real asyncpg
    exception — the one with `constraint_name` — hangs off it as `__cause__`. Read the wrapper for
    the sqlstate, the cause for the name, and fall back to the message text, which always quotes
    the constraint, so an adapter change cannot silently turn every violation into a generic 409.
    """
    orig = getattr(ex, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    cause = getattr(orig, "__cause__", None)
    constraint = (getattr(cause, "constraint_name", None)
                  or getattr(orig, "constraint_name", None))
    if not constraint:
        found = _CONSTRAINT_IN_TEXT.search(str(orig))
        constraint = found.group(1) if found else None
    message = _CONSTRAINT_MESSAGES.get(constraint) or _GENERIC.get(
        sqlstate, "The values sent conflict with data already stored.")
    return _SQLSTATE_STATUS.get(sqlstate, status.HTTP_409_CONFLICT), message


# ==============================================================================================
# serialization
# ==============================================================================================
# A SERIALIZER THAT READS `updated_at` NEEDS THE ROW REFRESHED AFTER AN UPDATE. The column's
# onupdate is a SQL expression, so SQLAlchemy expires the attribute once the flush has run and
# reading it lazily issues IO — which, outside the greenlet the async session runs in, raises
# MissingGreenlet and turns a working PATCH into a 500. `lease_json` and `policy_json` are the two
# that expose timestamps; their handlers call `session.refresh(row, [..., "updated_at"])`.

def num(v: Optional[Decimal]) -> Optional[float]:
    return None if v is None else float(v)


def iso(v) -> Optional[str]:
    return None if v is None else v.isoformat()


def enum_value(v: Any) -> Any:
    return v.value if hasattr(v, "value") else v


def contact_json(c: PartyContact) -> dict:
    return {"id": c.id, "company": c.company, "contact": c.contact,
            "email": c.email, "phone": c.phone, "note": c.note}


def party_json(p: Optional[Party], *, contacts: bool = True) -> Optional[dict]:
    if p is None:
        return None
    out = {"id": p.id, "name": p.name, "details": p.details}
    if contacts:
        out["contacts"] = [contact_json(c) for c in p.contacts]
    return out


def airline_json(a: Optional[Airline]) -> Optional[dict]:
    if a is None:
        return None
    return {"id": a.id, "airline_name": a.airline_name, "icao": a.icao, "iata": a.iata,
            "is_asg": a.is_asg, "logo_url": a.logo_url}


def aircraft_type_json(t: Optional[AircraftType]) -> Optional[dict]:
    return type_json(t)


# ==============================================================================================
# the outline drawings
# ==============================================================================================
# `fleet.aircraft_type.template_url` is an object, `{airborne, on_the_ground}`, and the column's
# CHECK allows exactly one representation of "nothing recorded": SQL NULL. These three functions
# are the only places that know it, so the routers never assemble the shape by hand.

def template_urls_json(value: Optional[dict]) -> dict:
    """What a READER sees: always both keys, even when nothing is recorded.

    A client that can rely on `template_url.airborne` existing never has to branch on null, and
    the information is not lost by it — the column forbids an object with both views null, so
    `{airborne: null, on_the_ground: null}` and SQL NULL mean the same thing.
    """
    value = value or {}
    return {view: value.get(view) for view in TEMPLATE_VIEWS}


def normalize_template_urls(payload: Optional[dict]) -> Optional[dict]:
    """What a WRITER stores: both keys, or NULL when no view has a URL.

    Collapsing the all-empty object to NULL is what keeps the reader's promise true, and an empty
    string is treated as no URL — a cleared form field must not become a link to nowhere.
    """
    if payload is None:
        return None
    out = {view: ((payload.get(view) or "").strip() or None) for view in TEMPLATE_VIEWS}
    return out if any(out.values()) else None


def merge_template_urls(current: Optional[dict], patch: dict) -> Optional[dict]:
    """PATCH, per VIEW. Only the views actually sent change.

    This is the whole reason the merge exists: uploading a new ground drawing must not wipe the
    airborne one the caller never mentioned. Sending a view explicitly as null clears that view;
    clearing both leaves the column NULL.
    """
    merged = dict(current or {})
    for view in TEMPLATE_VIEWS:
        if view in patch:
            merged[view] = patch[view]
    return normalize_template_urls(merged)


def type_json(t) -> Optional[dict]:
    """An aircraft type or an engine type — nearly the same shape. An AIRFRAME type also carries
    its category, which is part of its identity: 'Airbus A300-600' alone does not say whether the
    row is the freighter or the passenger aircraft, and `label` spells the whole thing out so a
    dropdown does not have to assemble it."""
    if t is None:
        return None
    out = {"id": t.id, "manufacturer": t.manufacturer, "master_series": t.master_series}
    if hasattr(t, "category"):
        out["category"] = enum_value(t.category)
        out["label"] = " ".join(x for x in (
            t.manufacturer, t.master_series, enum_value(t.category).capitalize()) if x)
    if hasattr(t, "template_url"):
        out["template_url"] = template_urls_json(t.template_url)
    return out


def engine_json(e: AircraftEngine, *, fitted: bool = False) -> dict:
    return {"id": e.id, "position": e.position, "engine_type": type_json(e.engine_type),
            "msn": e.msn, "installed_on": iso(e.installed_on), "details": e.details,
            "fitted": fitted}


def fitted_engine_ids(engines: Iterable[AircraftEngine]) -> set:
    """The newest installation per position — what is actually bolted on right now. Mirrors the
    DISTINCT ON in the model docstring; an undated row sorts below any dated one."""
    newest: dict[int, AircraftEngine] = {}
    for e in engines:
        current = newest.get(e.position)
        if current is None or (e.installed_on or date.min, e.id) > (current.installed_on or date.min, current.id):
            newest[e.position] = e
    return {e.id for e in newest.values()}


def service_json(v: Optional[ServiceInfo]) -> dict:
    """The service block. A missing row reads as the defaults rather than as nulls — the API
    creates one with every aircraft, so an absent row means the aircraft predates that, not that
    somebody cleared the fields."""
    if v is None:
        return {"id": None, "agreed_value_fixed": False, "source": "cirium", "status": "insured",
                "usage_status": None, "lease_currency": "USD", "policy_currency": "USD",
                "recorded": False}
    return {"id": v.id, "agreed_value_fixed": v.agreed_value_fixed,
            "source": enum_value(v.source), "status": enum_value(v.status),
            "usage_status": v.usage_status, "lease_currency": v.lease_currency,
            "policy_currency": v.policy_currency, "recorded": True}


def aircraft_json(a: Optional[Aircraft], *, engines: bool = True) -> Optional[dict]:
    if a is None:
        return None
    out = {
        "id": a.id,
        "registration": a.registration,
        "msn": a.msn,
        "aircraft_type": aircraft_type_json(a.aircraft_type),
        "airline": airline_json(a.airline),
        "service": service_json(a.service),
    }
    if engines:
        fitted = fitted_engine_ids(a.engines)
        out["engines"] = [engine_json(e, fitted=e.id in fitted) for e in a.engines]
    return out


def agreement_json(g: Optional[Agreement]) -> Optional[dict]:
    if g is None:
        return None
    return {
        "id": g.id,
        "name": g.name,
        "start_date": iso(g.start_date),
        "lessor": party_json(g.lessor, contacts=False),
        "alternative_contract_party": g.alternative_contract_party,
        "other_contracts": g.other_contracts,
    }


def lease_json(l: Optional[AircraftLease], *, on: Optional[date] = None,
               aircraft: Optional[Aircraft] = None,
               service: Optional[ServiceInfo] = None) -> Optional[dict]:
    """`agreed_value_calculated` is the formula applied at `on` (today by default) beside the
    figure the schedule stated, so a divergence is visible instead of silently resolved.

    Two of its inputs live in the service block, not on the lease: whether the value depreciates at
    all (`agreed_value_fixed`) and which currency the amounts are in. Pass the aircraft — its
    `service` is eagerly loaded — or the row itself.
    """
    if l is None:
        return None
    on = on or date.today()
    if service is None and aircraft is not None:
        service = aircraft.service
    return {
        "id": l.id,
        "aircraft": aircraft_json(aircraft, engines=False) if aircraft is not None else None,
        "aircraft_id": l.aircraft_id,
        "agreement": agreement_json(l.agreement),
        "effective_date": iso(l.effective_date),
        "agreed_value_preliminary": num(l.agreed_value_preliminary),
        "agreed_value_final": num(l.agreed_value_final),
        "agreed_value_calculated": agreed_value_at(
            l.agreed_value_preliminary, l.depreciation_ratio, l.depreciation_start_date,
            service.agreed_value_fixed if service is not None else False, on),
        "agreed_value_as_of": iso(on),
        "depreciation_ratio": num(l.depreciation_ratio),
        "depreciation_start_date": iso(l.depreciation_start_date),
        "combined_single_limit": num(l.combined_single_limit),
        "hull_spares_war_excess_liability": num(l.hull_spares_war_excess_liability),
        "hull_deductible_buy_down": num(l.hull_deductible_buy_down),
        "currency": service.lease_currency if service is not None else None,
        "created_at": iso(l.created_at),
        "updated_at": iso(l.updated_at),
    }


def policy_json(p: Optional[Policy]) -> Optional[dict]:
    if p is None:
        return None
    return {
        "id": p.id,
        "insured": party_json(p.insured, contacts=False),
        "reinsured": party_json(p.reinsured, contacts=False),
        "retrocedent": party_json(p.retrocedent, contacts=False),
        "period_from": iso(p.period_from),
        "period_to": iso(p.period_to),
        "period": f"{iso(p.period_from)}..{iso(p.period_to) or ''}",
        "hull_all_risks_deductible": num(p.hull_all_risks_deductible),
        "spares_deductible": num(p.spares_deductible),
        "hull_deductible_buy_down": num(p.hull_deductible_buy_down),
        "hull_deductible_aggregate": num(p.hull_deductible_aggregate),
        "combined_single_limit": num(p.combined_single_limit),
        "hull_war_overall_limit": num(p.hull_war_overall_limit),
        "hull_spares_limit": num(p.hull_spares_limit),
        "hull_spares_war_excess_liability": num(p.hull_spares_war_excess_liability),
        "hull_war_confiscation_limit": num(p.hull_war_confiscation_limit),
        "hull_war_confiscation_limit_selected_country": num(p.hull_war_confiscation_limit_selected_country),
        "selected_country": p.selected_country,
        "reinsured_amount": num(p.reinsured_amount),
        "cut_through_clause": p.cut_through_clause,
        "created_at": iso(p.created_at),
        "updated_at": iso(p.updated_at),
    }


def coverage_json(c: Optional[Coverage], *, aircraft: Optional[Aircraft] = None) -> Optional[dict]:
    if c is None:
        return None
    return {
        "id": c.id,
        "aircraft_id": c.aircraft_id,
        "aircraft": aircraft_json(aircraft, engines=False) if aircraft is not None else None,
        "policy": policy_json(c.policy),
        "covered_from": iso(c.covered_from),
        "covered_to": iso(c.covered_to),
    }


# ==============================================================================================
# lease and policy IN FORCE on a day
# ==============================================================================================

def lease_in_force(leases: Iterable[AircraftLease], on: date) -> Optional[AircraftLease]:
    """A lease row has no end date: the terms in force are the newest row not later than `on`.
    Same rule as the engines, and the same reason — the sequence of rows IS the business history."""
    candidates = [l for l in leases if l.effective_date <= on]
    return max(candidates, key=lambda l: (l.effective_date, l.id), default=None)


def covers(c: Coverage, on: date) -> bool:
    return c.covered_from <= on and (c.covered_to is None or c.covered_to >= on)


# ==============================================================================================
# audit diffs — the "what changed" panel
# ==============================================================================================
# The trigger stores whole-row JSONB snapshots, which is the right thing to persist (self-contained,
# survives schema drift) and the wrong thing to render. These turn a snapshot pair into a list of
# changed fields with foreign keys already resolved to names.

_DIFF_SKIP = {"id", "created_at", "updated_at"}

# JSONB column -> (lookup kind, field name shown to the client)
FK_LABELS = {
    "airline_id": ("airline", "airline"),
    "aircraft_id": ("aircraft", "aircraft"),
    "aircraft_type_id": ("aircraft_type", "aircraft_type"),
    "engine_type_id": ("engine_type", "engine_type"),
    "party_id": ("party", "party"),
    "lessor_id": ("party", "lessor"),
    "insured_id": ("party", "insured"),
    "reinsured_id": ("party", "reinsured"),
    "retrocedent_id": ("party", "retrocedent"),
    "agreement_id": ("agreement", "agreement"),
    "policy_id": ("policy", "policy"),
}


async def resolve_fk_labels(session, rows: Iterable[Optional[dict]]) -> dict:
    """Bulk-load the display name behind every foreign key mentioned in a batch of snapshots.

    Returns {(kind, id): label}. One query per KIND, not per row — an aircraft's history is dozens
    of snapshots repeating the same handful of ids.
    """
    wanted: dict[str, set] = {}
    for row in rows:
        if not row:
            continue
        for column, (kind, _) in FK_LABELS.items():
            value = row.get(column)
            if isinstance(value, int):
                wanted.setdefault(kind, set()).add(value)
    if not wanted:
        return {}

    labels: dict = {}
    if wanted.get("party"):
        for pid, name in (await session.execute(
            select(Party.id, Party.name).where(Party.id.in_(wanted["party"]))
        )).all():
            labels[("party", pid)] = name
    if wanted.get("airline"):
        for aid, name in (await session.execute(
            select(Airline.id, Airline.airline_name).where(Airline.id.in_(wanted["airline"]))
        )).all():
            labels[("airline", aid)] = name
    if wanted.get("aircraft"):
        for aid, reg, msn in (await session.execute(
            select(Aircraft.id, Aircraft.registration, Aircraft.msn)
            .where(Aircraft.id.in_(wanted["aircraft"]))
        )).all():
            labels[("aircraft", aid)] = f"{reg} (MSN {msn})" if msn else reg
    if wanted.get("aircraft_type"):
        # The CATEGORY belongs in the label. Without it, re-pointing an aircraft from the
        # passenger A320 to the cargo A320 renders as "Airbus A320 -> Airbus A320": a real change
        # that reads as no change at all, which is the one thing a change log must never do.
        for tid, manuf, series, category in (await session.execute(
            select(AircraftType.id, AircraftType.manufacturer, AircraftType.master_series,
                   AircraftType.category)
            .where(AircraftType.id.in_(wanted["aircraft_type"]))
        )).all():
            labels[("aircraft_type", tid)] = " ".join(
                x for x in (manuf, series, enum_value(category).capitalize()) if x)
    if wanted.get("engine_type"):
        for eid, manuf, series in (await session.execute(
            select(EngineType.id, EngineType.manufacturer, EngineType.master_series)
            .where(EngineType.id.in_(wanted["engine_type"]))
        )).all():
            labels[("engine_type", eid)] = f"{manuf} {series}" if manuf else series
    if wanted.get("agreement"):
        for gid, name, start in (await session.execute(
            select(Agreement.id, Agreement.name, Agreement.start_date)
            .where(Agreement.id.in_(wanted["agreement"]))
        )).all():
            labels[("agreement", gid)] = f"{name} ({iso(start)})" if start else name
    if wanted.get("policy"):
        for pid, pfrom, pto in (await session.execute(
            select(Policy.id, Policy.period_from, Policy.period_to)
            .where(Policy.id.in_(wanted["policy"]))
        )).all():
            labels[("policy", pid)] = f"{iso(pfrom)}..{iso(pto) or ''}"
    return labels


def diff_rows(old_row: Optional[dict], new_row: Optional[dict], labels: dict) -> list[dict]:
    """Field-level diff of two snapshots, foreign keys resolved to names.

    INSERT (old is None) reports every field actually filled in; DELETE (new is None) reports what
    was lost. Each entry is `{field, old, new}`, ready to render as "field: old -> new".
    """
    old_row = old_row or {}
    new_row = new_row or {}
    out = []
    for column in sorted(set(old_row) | set(new_row)):
        if column in _DIFF_SKIP:
            continue
        before, after = old_row.get(column), new_row.get(column)
        if before == after:
            continue
        kind_field = FK_LABELS.get(column)
        if kind_field is not None:
            kind, field = kind_field
            before = labels.get((kind, before)) if before is not None else None
            after = labels.get((kind, after)) if after is not None else None
            if before == after:      # both ids resolved to the same label, or to nothing
                continue
        else:
            field = column
        out.append({"field": field, "old": before, "new": after})
    return out


def audit_entry(row, labels: dict) -> dict:
    """One change-log row as the API returns it: the readable diff plus the raw snapshots, so a
    client that wants a field the diff skipped can still reach it."""
    return {
        "id": row.id,
        "schema": row.schema_name,
        "table": row.table_name,
        "row_id": row.row_id,
        "operation": row.operation,
        "changed_at": iso(row.changed_at),
        "changed_by": row.changed_by,
        "changes": diff_rows(row.old_row, row.new_row, labels),
        "old_row": row.old_row,
        "new_row": row.new_row,
    }
