"""Plumbing shared by the two insurance routers (Insurance.py = policies/records, Claims.py).

Everything here is domain logic that both routers need and neither owns: normalising names the way
the STORED generated columns do, finding-or-creating the reference rows, telling the audit triggers
who the actor is, and turning an audit row pair into the field-level diff the UI renders.
"""
from decimal import Decimal
from typing import Optional, Any, Iterable

from sqlalchemy import select, text

from Database import ApiToken
from Database.ApiModels import (
    Airlines, Aircrafts, AircraftTypes, EngineTypes, Parties, InsurancePolicies,
)

# api.parties holds every non-airline entity the domain names; the flag says which roles it has been
# seen in. Flags accumulate and never restrict use — they exist so the portal can filter autocomplete.
PARTY_ROLE_FLAGS = {
    "lessee": "is_lessee",
    "lessor": "is_lessor",
    "surveyor": "is_surveyor",
    "leader": "is_insurer",
}


# ==============================================================================================
# normalisation — must mirror the STORED generated columns exactly
# ==============================================================================================

def norm(value: str) -> str:
    """upper + trim — matches the `*_normalized` columns on parties/aircraft_types/engine_types."""
    return value.strip().upper()


def norm_reg(value: str) -> str:
    """upper + strip separators — matches api.aircrafts.registration_normalized ('YLLTD' == 'YL-LTD')."""
    return "".join(ch for ch in value.upper() if ch.isalnum())


# ==============================================================================================
# find-or-create helpers for the reference tables
# ==============================================================================================
# All of them match on the STORED generated `*_normalized` column, which also carries the unique
# constraint — so 'AerCap' and 'AERCAP ' can never become two rows.

async def get_or_create_airline(session, name: Optional[str]) -> Optional[Airlines]:
    if not name or not name.strip():
        return None
    # api.airlines predates this domain and has no normalized column — match case-insensitively.
    row = (await session.execute(
        select(Airlines).where(Airlines.airline_name.ilike(name.strip())).limit(1)
    )).scalar_one_or_none()
    if row is None:
        row = Airlines(airline_name=name.strip())
        session.add(row)
        await session.flush()
    return row


async def get_or_create_party(session, name: Optional[str], *, role: str) -> Optional[Parties]:
    """One table for every role — the same entity is a lessor on one aircraft and a lessee on
    another, and an insurer can also be a surveyor. `role` only decides which `is_*` hint gets set."""
    if not name or not name.strip():
        return None
    flag = PARTY_ROLE_FLAGS[role]
    key = norm(name)
    row = (await session.execute(
        select(Parties).where(Parties.name_normalized == key)
    )).scalar_one_or_none()
    if row is None:
        row = Parties(name=name.strip(), **{flag: True})
        session.add(row)
        await session.flush()
    elif not getattr(row, flag):
        setattr(row, flag, True)
    return row


async def get_or_create_aircraft_type(session, name: Optional[str]) -> Optional[AircraftTypes]:
    if not name or not name.strip():
        return None
    key = norm(name)
    row = (await session.execute(
        select(AircraftTypes).where(AircraftTypes.name_normalized == key)
    )).scalar_one_or_none()
    if row is None:
        row = AircraftTypes(name=name.strip())
        session.add(row)
        await session.flush()
    return row


async def get_or_create_engine_type(session, name: Optional[str]) -> Optional[EngineTypes]:
    if not name or not name.strip():
        return None
    key = norm(name)
    row = (await session.execute(
        select(EngineTypes).where(EngineTypes.name_normalized == key)
    )).scalar_one_or_none()
    if row is None:
        row = EngineTypes(name=name.strip())
        session.add(row)
        await session.flush()
    return row


async def find_aircraft(session, *, registration: Optional[str] = None,
                        msn: Optional[str] = None) -> Optional[Aircrafts]:
    """MSN wins: it is the airframe's real identity and a tail number can be reused by another
    aircraft after a re-registration."""
    if msn and msn.strip():
        row = (await session.execute(
            select(Aircrafts).where(Aircrafts.msn == msn.strip())
        )).scalar_one_or_none()
        if row is not None:
            return row
    if registration and registration.strip():
        return (await session.execute(
            select(Aircrafts)
            .where(Aircrafts.registration_normalized == norm_reg(registration))
            .order_by(Aircrafts.id)
            .limit(1)
        )).scalar_one_or_none()
    return None


async def set_actor(session, token: Optional[ApiToken]) -> None:
    """Tell the audit triggers who is making the change. Transaction-local (the `true` argument), so
    it cannot leak into another request sharing the pooled connection."""
    actor = token.name if token is not None else "service-token"
    await session.execute(text("SELECT set_config('app.actor', :actor, true)"), {"actor": actor})


# ==============================================================================================
# serialization
# ==============================================================================================

def num(v: Optional[Decimal]) -> Optional[float]:
    return None if v is None else float(v)


def enum_value(v: Any) -> Any:
    return v.value if hasattr(v, "value") else v


def party_json(p: Optional[Parties]) -> Optional[dict]:
    if p is None:
        return None
    return {
        "id": p.id,
        "name": p.name,
        "country": p.country,
        "roles": [role for role, flag in PARTY_ROLE_FLAGS.items() if getattr(p, flag, False)],
    }


def policy_json(p: Optional[InsurancePolicies]) -> Optional[dict]:
    if p is None:
        return None
    return {
        "id": p.id,
        "policy_number": p.policy_number,
        "policy_from": p.policy_from.isoformat(),
        "policy_to": p.policy_to.isoformat() if p.policy_to else None,
        # what the source schedule calls `policy_period`, rendered once so the UI never builds it
        "policy_period": _period_label(p),
        "currency": p.currency,
        "combined_single_limit": num(p.combined_single_limit),
        "airline": p.airline.airline_name if p.airline else None,
    }


def _period_label(p: InsurancePolicies) -> str:
    return f"{p.policy_from.isoformat()}..{p.policy_to.isoformat() if p.policy_to else ''}"


# ==============================================================================================
# audit diffs — what the frontend's "previous values were …" info button renders
# ==============================================================================================
# The triggers store whole-row JSONB snapshots, which is the right thing to persist (self-contained,
# survives schema drift) but not what a UI wants to read. These helpers turn a snapshot pair into a
# list of changed fields with foreign keys already resolved to names.

# audit columns and surrogate keys never make an interesting diff entry
_DIFF_SKIP = {"id", "created_at", "updated_at"}

# JSONB column name -> (label kind, field name shown to the client)
FK_LABELS = {
    "aircraft_id": ("aircraft", "aircraft"),
    "airline_id": ("airline", "airline"),
    "policy_id": ("policy", "policy"),
    "lessee_id": ("party", "lessee"),
    "lessor_id": ("party", "lessor"),
    "surveyor_id": ("party", "surveyor"),
    "leader_id": ("party", "leader"),
}


async def resolve_fk_labels(session, rows: Iterable[Optional[dict]]) -> dict:
    """Bulk-load the display names for every foreign key mentioned in a batch of audit snapshots.

    Returns {(kind, id): label}. One query per kind, not per row — a claim's history can be dozens
    of snapshots and they overwhelmingly repeat the same handful of ids.
    """
    wanted: dict[str, set] = {}
    for row in rows:
        if not row:
            continue
        for column, (kind, _) in FK_LABELS.items():
            value = row.get(column)
            if isinstance(value, int):
                wanted.setdefault(kind, set()).add(value)

    labels: dict = {}
    if wanted.get("party"):
        for pid, name in (await session.execute(
            select(Parties.id, Parties.name).where(Parties.id.in_(wanted["party"]))
        )).all():
            labels[("party", pid)] = name
    if wanted.get("airline"):
        for aid, name in (await session.execute(
            select(Airlines.id, Airlines.airline_name).where(Airlines.id.in_(wanted["airline"]))
        )).all():
            labels[("airline", aid)] = name
    if wanted.get("aircraft"):
        for aid, reg, msn in (await session.execute(
            select(Aircrafts.id, Aircrafts.registration, Aircrafts.msn)
            .where(Aircrafts.id.in_(wanted["aircraft"]))
        )).all():
            labels[("aircraft", aid)] = reg if not msn else f"{reg} (MSN {msn})"
    if wanted.get("policy"):
        for pid, number, pfrom, pto in (await session.execute(
            select(InsurancePolicies.id, InsurancePolicies.policy_number,
                   InsurancePolicies.policy_from, InsurancePolicies.policy_to)
            .where(InsurancePolicies.id.in_(wanted["policy"]))
        )).all():
            period = f"{pfrom.isoformat()}..{pto.isoformat() if pto else ''}"
            labels[("policy", pid)] = f"{number} ({period})" if number else period
    return labels


def diff_rows(old_row: Optional[dict], new_row: Optional[dict], labels: dict) -> list[dict]:
    """Field-level diff of two audit snapshots, foreign keys resolved to names.

    INSERT (old is None) reports every field that was actually filled in; DELETE (new is None)
    reports the values that were lost. Each entry is `{field, old, new}` and is ready to render as
    "field: old -> new" without further lookups.
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
            if before == after:      # both ids resolved to the same (or to nothing) — not a change
                continue
        else:
            field = column
        out.append({"field": field, "old": before, "new": after})
    return out


def audit_entry(row, labels: dict, *, id_field: str) -> dict:
    """One audit row as the API returns it: the human-readable diff plus the raw snapshots, so a
    client that wants a field the diff skipped can still reach it."""
    return {
        "id": row.id,
        id_field: getattr(row, id_field),
        "operation": row.operation,
        "changed_at": row.changed_at.isoformat(),
        "changed_by": row.changed_by,
        "changes": diff_rows(row.old_row, row.new_row, labels),
        "old_row": row.old_row,
        "new_row": row.new_row,
    }
