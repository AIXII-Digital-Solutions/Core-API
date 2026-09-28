"""Insurance policies and per-aircraft coverage: `/policy/policies`, `/policy/coverage`.

This side holds what the insurer PROVIDES. Its twin is `/leasing`, which holds what the lease
REQUIRES; `GET /policy/coverage/compare` puts the two side by side, which is the question the whole
split exists to answer.

A POLICY COVERS A FLEET. Every limit and deductible lives on the policy, once, because that is how
it is written; `policy.coverage` says which airframes it covers and over what window. An aircraft
holds ONE policy at a time — a second overlapping coverage is refused by the database, not by a
check here.

A POLICY NAMES SEVERAL PARTIES. Insured, reinsured and retrocedent are each a LIST — co-insured
group companies, a panel of reinsurers — held in policy.policy_party in schedule order.

ONE REQUEST PER POLICY. The aircraft a policy covers are sent WITH it (`aircraft` on POST and
PATCH) or added in one go at POST /policy/coverage/bulk: all in one transaction, never one request
per airframe.

RENEWAL IS THE NORMAL CASE. `POST /policy/policies/{id}/renew` creates next year's policy with the
same terms and carries the chosen aircraft onto it, leaving the expiring policy and its coverage
rows untouched. That is what makes an aircraft's insurance history readable years later: a chain of
coverage rows, each pointing at the contract that was in force.
"""
from datetime import date, timedelta
from decimal import Decimal
from typing import Annotated, Optional, Union

from fastapi import Request, Response, Depends, Query, status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, StrictInt, StringConstraints, field_validator, model_validator
from sqlalchemy import select, func, or_, exists
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload, raiseload

from Config import setup_logger
from settings import Router
from Database import ApiToken
from Database.FleetModels import Aircraft
from Database.LeasingModels import AircraftLease
from Database.PolicyModels import Policy, Coverage, PolicyParty, PartyRole
from api_auth import authorize, SCOPE_INSURANCE_READ, SCOPE_INSURANCE_WRITE
from Utils import success_response, warning_response, error_response
from Utils.ResponsesFunc import build_responses
from Utils.DomainCache import FLEET, PARTY, cached, invalidate
from Utils.DomainCommon import (
    reload_with, AIRCRAFT_BRIEF, POLICY_LOAD, COVERAGE_LOAD, page_with_total,
    DB, set_actor, apply_sort, SortError, integrity_error, find_aircraft, norm_reg,
    resolve_parties, PartyRefError, policy_parties,
    policy_json, coverage_json, aircraft_json, lease_in_force, num, enum_value,
)

logger = setup_logger("policy_api")

router = Router(prefix="/policy", tags=["Policy"])

_POLICY_SORTS = {
    "period_from": Policy.period_from,
    "period_to": Policy.period_to,
    "hull_all_risks_deductible": Policy.hull_all_risks_deductible,
    "combined_single_limit": Policy.combined_single_limit,
    "hull_war_overall_limit": Policy.hull_war_overall_limit,
    "reinsured_amount": Policy.reinsured_amount,
    "created_at": Policy.created_at,
    "updated_at": Policy.updated_at,
}
_COVERAGE_SORTS = {
    "covered_from": Coverage.covered_from,
    "covered_to": Coverage.covered_to,
    "created_at": Coverage.created_at,
}

_READ = [Depends(authorize(SCOPE_INSURANCE_READ))]
_OK = {status.HTTP_200_OK, status.HTTP_400_BAD_REQUEST, status.HTTP_404_NOT_FOUND,
       status.HTTP_409_CONFLICT, status.HTTP_500_INTERNAL_SERVER_ERROR}

# the parties are a collection per policy: selectin, declared once in DomainCommon
_POLICY_LOAD = POLICY_LOAD
_COVERAGE_LOAD = COVERAGE_LOAD
_ROLES = (PartyRole.INSURED, PartyRole.REINSURED, PartyRole.RETROCEDENT)

# The three columns that exist on BOTH the lease and the policy, so required and provided cover can
# be compared. Kept in one place so /coverage/compare and the docs cannot drift apart.
COMPARED = ("combined_single_limit", "hull_spares_war_excess_liability", "hull_deductible_buy_down")


# ==============================================================================================
# bodies
# ==============================================================================================

PartyRef = Union[StrictInt, Annotated[str, StringConstraints(strip_whitespace=True, min_length=1,
                                                              max_length=256)]]

_PARTY_DESC = ("A LIST, in the order the schedule gives them. Each entry is a ref.party id (a "
               "number) or a name (a string, found or created). A single value is accepted too.")


def _listify(value):
    """One party where a list is expected is still one party: accept it, as the API used to."""
    if value is None or isinstance(value, list):
        return value
    return [value]


class CoverageItem(BaseModel):
    """One aircraft to put on a policy: by id, or looked up by MSN / registration. The window
    defaults to the policy period — set it for a mid-term delivery or a redelivery."""
    aircraft_id: Optional[int] = None
    registration: Optional[str] = Field(default=None, max_length=32)
    msn: Optional[str] = Field(default=None, max_length=64)
    covered_from: Optional[date] = Field(default=None, description="Defaults to period_from.")
    covered_to: Optional[date] = Field(default=None, description="Defaults to period_to.")

    @model_validator(mode="after")
    def _check(self):
        if self.aircraft_id is None and not (self.registration or self.msn):
            raise ValueError("give `aircraft_id`, or `registration` / `msn` to look one up")
        if self.covered_from and self.covered_to and self.covered_to < self.covered_from:
            raise ValueError("`covered_to` must not be earlier than `covered_from`")
        return self


class _PolicyTerms(BaseModel):
    hull_all_risks_deductible: Optional[Decimal] = Field(default=None, ge=0)
    spares_deductible: Optional[Decimal] = Field(default=None, ge=0)
    hull_deductible_buy_down: Optional[Decimal] = Field(default=None, ge=0)
    hull_deductible_aggregate: Optional[Decimal] = Field(default=None, ge=0)
    combined_single_limit: Optional[Decimal] = Field(default=None, ge=0)
    hull_war_overall_limit: Optional[Decimal] = Field(default=None, ge=0)
    hull_spares_limit: Optional[Decimal] = Field(default=None, ge=0)
    hull_spares_war_excess_liability: Optional[Decimal] = Field(default=None, ge=0)
    hull_war_confiscation_limit: Optional[Decimal] = Field(default=None, ge=0)
    hull_war_confiscation_limit_selected_country: Optional[Decimal] = Field(default=None, ge=0)
    selected_country: Optional[str] = Field(
        default=None, max_length=128,
        description="The one territory the reduced confiscation limit applies to, e.g. 'Russia'.")
    reinsured_amount: Optional[Decimal] = Field(
        default=None, ge=0, le=100,
        description="PERCENT ceded, of `reinsured_amount_of` (97.5 of 100 = 97.5 %).")
    reinsured_amount_of: Optional[Decimal] = Field(
        default=None, ge=0, le=100,
        description="The share, in percent, that `reinsured_amount` is taken of (the 100 in "
                    "'97.5 of 100').")
    cut_through_clause: Optional[str] = None


class PolicyIn(_PolicyTerms):
    """Parties are ids or NAMES, found or created in ref.party — the insured on an aviation policy
    is routinely a lessor or a holding company rather than the operating airline."""
    insured: list[PartyRef] = Field(min_length=1, max_length=50, description=_PARTY_DESC)
    reinsured: list[PartyRef] = Field(default_factory=list, max_length=50, description=_PARTY_DESC)
    retrocedent: list[PartyRef] = Field(default_factory=list, max_length=50,
                                        description=_PARTY_DESC)
    period_from: date
    period_to: Optional[date] = None
    aircraft: list[CoverageItem] = Field(
        default_factory=list, max_length=1000,
        description="The aircraft this policy covers, created with it in the same transaction.")

    @model_validator(mode="before")
    @classmethod
    def _legacy(cls, value):
        # `insured_id` and single values are how the API used to take parties
        if isinstance(value, dict):
            value = dict(value)
            legacy = value.pop("insured_id", None)
            for key in ("insured", "reinsured", "retrocedent"):
                value[key] = _listify(value.get(key))
            if legacy is not None and not value.get("insured"):
                value["insured"] = [legacy]
            for key in ("insured", "reinsured", "retrocedent"):
                if value.get(key) is None:
                    value.pop(key, None)
        return value

    @model_validator(mode="after")
    def _check(self):
        if self.period_to is not None and self.period_to < self.period_from:
            raise ValueError("`period_to` must not be earlier than `period_from`")
        return self


class PolicyPatch(_PolicyTerms):
    """Only the fields sent are touched. A party list that is sent REPLACES that role's list
    (`[]` empties reinsured or retrocedent; insured cannot be empty)."""
    insured: Optional[list[PartyRef]] = Field(default=None, min_length=1, max_length=50,
                                              description=_PARTY_DESC)
    reinsured: Optional[list[PartyRef]] = Field(default=None, max_length=50, description=_PARTY_DESC)
    retrocedent: Optional[list[PartyRef]] = Field(default=None, max_length=50,
                                                  description=_PARTY_DESC)
    period_from: Optional[date] = None
    period_to: Optional[date] = None
    aircraft: Optional[list[CoverageItem]] = Field(
        default=None, max_length=1000,
        description="The COMPLETE set of aircraft on this policy. Aircraft not yet on it are "
                    "added, aircraft left out are taken off (their coverage rows on THIS policy "
                    "are deleted), aircraft already on it keep their window unless the item sets "
                    "`covered_from` / `covered_to`. Omit the field to leave the aircraft alone.")

    @field_validator("insured", "reinsured", "retrocedent", mode="before")
    @classmethod
    def _one_or_many(cls, value):
        return _listify(value)

    @model_validator(mode="after")
    def _check(self):
        if "insured" in self.model_fields_set and not self.insured:
            raise ValueError("a policy must keep at least one insured")
        return self


class CoverageIn(BaseModel):
    policy_id: int
    aircraft_id: Optional[int] = None
    registration: Optional[str] = Field(default=None, max_length=32)
    msn: Optional[str] = Field(default=None, max_length=64)
    covered_from: Optional[date] = Field(
        default=None, description="Defaults to the policy's period_from — set it for a mid-term delivery.")
    covered_to: Optional[date] = Field(
        default=None, description="Defaults to the policy's period_to — set it for a redelivery.")

    @model_validator(mode="after")
    def _check(self):
        if self.aircraft_id is None and not (self.registration or self.msn):
            raise ValueError("give `aircraft_id`, or `registration` / `msn` to look one up")
        return self


class CoverageBulkIn(BaseModel):
    policy_id: int
    aircraft: list[CoverageItem] = Field(min_length=1, max_length=1000)


class CoveragePatch(BaseModel):
    covered_from: Optional[date] = None
    covered_to: Optional[date] = None


class RenewIn(BaseModel):
    """Next year's contract. Everything not overridden is copied from the expiring policy."""
    period_from: Optional[date] = Field(
        default=None, description="Defaults to the day after the expiring policy's period_to.")
    period_to: Optional[date] = None
    carry_aircraft: bool = Field(
        default=True, description="Move the expiring policy's aircraft onto the new one.")
    overrides: PolicyPatch = Field(
        default_factory=PolicyPatch,
        description="Anything to change on the new policy. `aircraft` is not accepted here — "
                    "`carry_aircraft` decides that.")

    @model_validator(mode="after")
    def _check(self):
        if self.overrides.aircraft is not None:
            raise ValueError("`overrides.aircraft` is not accepted: use `carry_aircraft`, then "
                             "PATCH the new policy's `aircraft`")
        return self


# ==============================================================================================
# writing the parts of a policy that are lists
# ==============================================================================================

class _ItemErrors(Exception):
    """Entries of a list the write cannot resolve, collected so every one is reported at once.
    Raised INSIDE the session, which rolls the whole write back, then rendered as the ordinary
    422 with `field` = `body.<list>.<index>...`."""

    def __init__(self, errors: list[dict]):
        super().__init__(f"{len(errors)} error(s)")
        self.errors = errors


class _Overlap(Exception):
    def __init__(self, clashes: list[str]):
        super().__init__(", ".join(clashes))
        self.clashes = clashes


def _err(loc: tuple, msg: str) -> dict:
    return {"loc": loc, "msg": msg, "type": "value_error"}


async def _write_parties(session, policy_id: int, role: PartyRole, refs: list,
                         existing: list[PolicyParty], errors: list[dict]) -> None:
    """Make `role` on the policy exactly `refs`, in that order. Rows already right are left
    alone and only the difference is written, so resending an unchanged list adds nothing to the
    audit log."""
    try:
        parties = await resolve_parties(session, refs)
    except PartyRefError as ex:
        errors.extend(_err(("body", role.value, i), msg) for i, msg in ex.errors)
        return
    wanted = {party.id: position for position, party in enumerate(parties, 1)}
    held = set()
    for link in existing:
        if link.party_id not in wanted:
            await session.delete(link)
            continue
        held.add(link.party_id)
        if link.position != wanted[link.party_id]:
            link.position = wanted[link.party_id]
    session.add_all(PolicyParty(policy_id=policy_id, party_id=pid, role=role, position=pos)
                    for pid, pos in wanted.items() if pid not in held)


async def _resolve_items(session, policy: Policy, items: list[CoverageItem], loc: tuple):
    """Each item as (aircraft_id, registration, covered_from, covered_to), the aircraft found in
    ONE query whatever mix of ids, MSNs and registrations was sent. Raises _ItemErrors."""
    ids = [it.aircraft_id for it in items if it.aircraft_id is not None]
    regs = [norm_reg(it.registration) for it in items
            if it.aircraft_id is None and it.registration]
    msns = [it.msn.strip() for it in items if it.aircraft_id is None and it.msn and it.msn.strip()]
    conds = [c for c in (Aircraft.id.in_(ids) if ids else None,
                         Aircraft.registration_normalized.in_(regs) if regs else None,
                         Aircraft.msn.in_(msns) if msns else None) if c is not None]
    rows = (await session.execute(
        select(Aircraft.id, Aircraft.registration, Aircraft.registration_normalized, Aircraft.msn)
        .where(or_(*conds)))).all()
    by_id = {r.id: r for r in rows}
    by_reg = {r.registration_normalized: r for r in rows}
    by_msn = {r.msn: r for r in rows if r.msn}

    errors, out, seen = [], [], {}
    for i, it in enumerate(items):
        if it.aircraft_id is not None:
            hit, field = by_id.get(it.aircraft_id), "aircraft_id"
        else:
            # MSN first, as everywhere in this domain: a tail number can be reissued
            hit = by_msn.get(it.msn.strip()) if it.msn and it.msn.strip() else None
            hit = hit or (by_reg.get(norm_reg(it.registration)) if it.registration else None)
            field = "registration" if it.registration else "msn"
        if hit is None:
            errors.append(_err((*loc, i, field),
                               "No such aircraft. Add the airframe at POST /fleet/aircraft first."))
            continue
        if hit.id in seen:
            errors.append(_err((*loc, i, field), f"{hit.registration} is already row {seen[hit.id]}."))
            continue
        seen[hit.id] = i
        start = it.covered_from or policy.period_from
        end = it.covered_to if it.covered_to is not None else policy.period_to
        if end is not None and end < start:
            errors.append(_err((*loc, i, "covered_to"),
                               "The window ends before it starts (check the policy period)."))
            continue
        out.append((hit.id, hit.registration, start, end, it))
    if errors:
        raise _ItemErrors(errors)
    return out


def _overlaps(a_from, a_to, b_from, b_to) -> bool:
    """Inclusive ranges, NULL end = open — the same test as ex_coverage_no_overlap."""
    return (b_to is None or a_from <= b_to) and (a_to is None or b_from <= a_to)


async def _insert_coverages(session, policy: Policy, resolved: list) -> int:
    """Insert coverage rows, having first checked — in ONE query — that none overlaps a coverage
    the aircraft already holds. The database would refuse it anyway (ex_coverage_no_overlap), but
    only for the first clash and without saying which aircraft; this names all of them."""
    if not resolved:
        return 0
    held = (await session.execute(
        select(Coverage.aircraft_id, Coverage.policy_id, Coverage.covered_from, Coverage.covered_to)
        .where(Coverage.aircraft_id.in_([r[0] for r in resolved])))).all()
    by_aircraft: dict[int, list] = {}
    for h in held:
        by_aircraft.setdefault(h.aircraft_id, []).append(h)
    clashes = [
        f"{reg} (policy {h.policy_id}, {h.covered_from.isoformat()}.."
        f"{h.covered_to.isoformat() if h.covered_to else ''})"
        for aid, reg, start, end, _ in resolved
        for h in by_aircraft.get(aid, [])
        if _overlaps(start, end, h.covered_from, h.covered_to)
    ]
    if clashes:
        raise _Overlap(clashes)
    session.add_all(Coverage(aircraft_id=aid, policy_id=policy.id, covered_from=start,
                             covered_to=end) for aid, _, start, end, _ in resolved)
    await session.flush()
    return len(resolved)


async def _sync_coverages(session, policy: Policy, items: list[CoverageItem]) -> None:
    """Make the policy cover exactly `items`: add, take off, and re-window only what differs."""
    resolved = await _resolve_items(session, policy, items, ("body", "aircraft"))
    wanted = {r[0]: r for r in resolved}
    current = (await session.execute(
        select(Coverage).where(Coverage.policy_id == policy.id))).scalars().all()
    on_policy = set()
    for c in current:
        want = wanted.get(c.aircraft_id)
        if want is None:
            await session.delete(c)
            continue
        on_policy.add(c.aircraft_id)
        item = want[4]
        if item.covered_from is not None:
            c.covered_from = item.covered_from
        if "covered_to" in item.model_fields_set:
            c.covered_to = item.covered_to
    await session.flush()
    await _insert_coverages(session, policy, [r for r in resolved if r[0] not in on_policy])


async def _policy_detail(session, policy_id: int) -> dict:
    """The policy with every aircraft it covers — what GET /policies/{id} returns, and what every
    write that can change either hands back."""
    row = (await session.execute(
        select(Policy).where(Policy.id == policy_id).options(*_POLICY_LOAD)
        .execution_options(populate_existing=True))).scalar_one()
    covered = (await session.execute(
        select(Coverage, Aircraft).join(Aircraft, Aircraft.id == Coverage.aircraft_id)
        .where(Coverage.policy_id == policy_id).options(*AIRCRAFT_BRIEF)
        .order_by(Aircraft.registration, Coverage.covered_from)
        .execution_options(populate_existing=True)
    )).all()
    data = policy_json(row)
    data["aircraft"] = [
        {"coverage_id": c.id, "covered_from": c.covered_from.isoformat(),
         "covered_to": c.covered_to.isoformat() if c.covered_to else None,
         "aircraft": aircraft_json(a, engines=False)}
        for c, a in covered
    ]
    return data


def _write_failed(request, response, ex):
    """The shared translation of a failed policy/coverage write."""
    if isinstance(ex, _ItemErrors):
        raise RequestValidationError(ex.errors)
    if isinstance(ex, _Overlap):
        return warning_response(
            request=request, response=response,
            msg=("Already covered over part of this period — an aircraft holds one policy at a "
                 f"time. Nothing was saved: {', '.join(ex.clashes)}"),
            status_code=status.HTTP_409_CONFLICT)
    code, msg = integrity_error(ex)
    return warning_response(request=request, response=response, msg=msg, status_code=code)


_WRITE_ERRORS = (_ItemErrors, _Overlap, IntegrityError)


# ==============================================================================================
# policies
# ==============================================================================================

@router.get(
    path="/policies",
    description=(
        "Insurance policies. `insured_id` narrows to the policies that party is insured on, "
        "`party_id` to those it appears on in any role; `on_date` keeps only the policies in force "
        "that day; `active` is shorthand for on_date=today. Returns `{items, total}`."
    ),
    responses=build_responses(include=_OK), dependencies=_READ,
)
async def list_policies(request: Request, response: Response,
                        insured_id: Optional[int] = Query(None),
                        party_id: Optional[int] = Query(None, description="Any role."),
                        on_date: Optional[date] = Query(None),
                        active: bool = Query(False, description="Shorthand for on_date=today."),
                        limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                        sort: Optional[str] = Query(None), order: Optional[str] = Query(None)):
    try:
        conds = []
        if insured_id is not None:
            conds.append(exists().where(PolicyParty.policy_id == Policy.id,
                                        PolicyParty.party_id == insured_id,
                                        PolicyParty.role == PartyRole.INSURED))
        if party_id is not None:
            conds.append(exists().where(PolicyParty.policy_id == Policy.id,
                                        PolicyParty.party_id == party_id))
        on = on_date or (date.today() if active else None)
        if on is not None:
            conds.append(Policy.period_from <= on)
            conds.append((Policy.period_to.is_(None)) | (Policy.period_to >= on))
        stmt = apply_sort(select(Policy).where(*conds).options(*_POLICY_LOAD),
                          sort=sort, order=order, sortmap=_POLICY_SORTS,
                          tiebreak=(Policy.period_from.desc(), Policy.id))
        async with request.app.state.db_client.read_session(DB) as session:
            rows, total = await page_with_total(
                session, stmt, limit=limit, offset=offset,
                count_stmt=select(func.count()).select_from(Policy).where(*conds))
            data = {"items": [policy_json(p) for p in rows], "total": total}
        return success_response(request=request, response=response, data=data)
    except SortError as _ex:
        return warning_response(request=request, response=response, msg=str(_ex))
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.post(
    path="/policies",
    description=(
        "Add a policy — with its aircraft, in ONE request. `insured` (at least one), `reinsured` "
        "and `retrocedent` are lists of party ids or names. `aircraft` puts airframes on it in "
        "the same transaction: all saved or none, and an aircraft already covered over part of "
        "the period is a 409 naming every such aircraft. `reinsured_amount` is a percent of "
        "`reinsured_amount_of`. Returns the policy with its aircraft."
    ),
    status_code=status.HTTP_201_CREATED,
    responses=build_responses(include=_OK | {status.HTTP_201_CREATED}),
)
async def create_policy(request: Request, response: Response, body: PolicyIn,
                        token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        async with request.app.state.db_client.session(DB) as session:
            await set_actor(session, token)
            row = Policy(**body.model_dump(
                exclude={"insured", "reinsured", "retrocedent", "aircraft"}))
            session.add(row)
            await session.flush()
            errors: list[dict] = []
            for role in _ROLES:
                await _write_parties(session, row.id, role, getattr(body, role.value), [], errors)
            if errors:
                raise _ItemErrors(errors)
            await session.flush()
            if body.aircraft:
                resolved = await _resolve_items(session, row, body.aircraft, ("body", "aircraft"))
                await _insert_coverages(session, row, resolved)
            data = await _policy_detail(session, row.id)
        await invalidate(request, PARTY)   # a counterparty may have been created
        return success_response(request=request, response=response, data=data,
                                msg=f"Policy created with {len(data['aircraft'])} aircraft",
                                status_code=status.HTTP_201_CREATED)
    except _WRITE_ERRORS as _ex:
        return _write_failed(request, response, _ex)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.get(path="/policies/{policy_id}",
            description="One policy with every aircraft covered by it.",
            responses=build_responses(include=_OK), dependencies=_READ)
async def get_policy(request: Request, response: Response, policy_id: int):
    try:
        async with request.app.state.db_client.read_session(DB) as session:
            found = (await session.execute(
                select(Policy.id).where(Policy.id == policy_id))).scalar_one_or_none()
            if found is None:
                return warning_response(request=request, response=response,
                                        msg=f"Policy {policy_id} not found",
                                        status_code=status.HTTP_404_NOT_FOUND)
            data = await _policy_detail(session, policy_id)
        return success_response(request=request, response=response, data=data)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.patch(path="/policies/{policy_id}",
              description="Endorse or correct a policy — terms, parties and aircraft in ONE "
                          "request. Only the fields sent are touched; a party list sent replaces "
                          "that role, and `aircraft` sent is the complete set (added / taken off "
                          "to match), all in one transaction. Previous values stay in the audit "
                          "log. A RENEWAL is not a patch — use /renew, so the expiring contract "
                          "keeps its own terms. Returns the policy with its aircraft.",
              responses=build_responses(include=_OK))
async def update_policy(request: Request, response: Response, policy_id: int, body: PolicyPatch,
                        token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        fields = body.model_dump(exclude_unset=True)
        async with request.app.state.db_client.session(DB) as session:
            await set_actor(session, token)
            row = (await session.execute(
                select(Policy).where(Policy.id == policy_id).options(*_POLICY_LOAD)
            )).scalar_one_or_none()
            if row is None:
                return warning_response(request=request, response=response,
                                        msg=f"Policy {policy_id} not found",
                                        status_code=status.HTTP_404_NOT_FOUND)
            fields.pop("aircraft", None)
            errors: list[dict] = []
            for role in _ROLES:
                if role.value in fields:
                    await _write_parties(session, row.id, role, fields.pop(role.value) or [],
                                         policy_parties(row, role), errors)
            if errors:
                raise _ItemErrors(errors)
            for key, value in fields.items():
                setattr(row, key, value)
            await session.flush()
            if body.aircraft is not None:
                await _sync_coverages(session, row, body.aircraft)
            # `updated_at` is computed by the database on UPDATE, so SQLAlchemy expires it after
            # the flush; the re-read below (populate_existing) is what makes serializing it safe.
            data = await _policy_detail(session, row.id)
        await invalidate(request, PARTY)   # a counterparty may have been created
        return success_response(request=request, response=response, data=data)
    except _WRITE_ERRORS as _ex:
        return _write_failed(request, response, _ex)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.post(
    path="/policies/{policy_id}/renew",
    description=(
        "Create next year's policy from this one and carry its aircraft across. The expiring "
        "policy and its coverage rows are NOT touched — that chain is the aircraft's insurance "
        "history. `period_from` defaults to the day after the expiring period ends; anything in "
        "`overrides` replaces the copied value. Refused when the expiring policy is open-ended and "
        "no `period_from` is given, because the two would overlap."
    ),
    responses=build_responses(include=_OK | {status.HTTP_201_CREATED}),
)
async def renew_policy(request: Request, response: Response, policy_id: int, body: RenewIn,
                       token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        async with request.app.state.db_client.session(DB) as session:
            await set_actor(session, token)
            old = (await session.execute(
                select(Policy).where(Policy.id == policy_id).options(*_POLICY_LOAD)
            )).scalar_one_or_none()
            if old is None:
                return warning_response(request=request, response=response,
                                        msg=f"Policy {policy_id} not found",
                                        status_code=status.HTTP_404_NOT_FOUND)
            period_from = body.period_from
            if period_from is None:
                if old.period_to is None:
                    return warning_response(
                        request=request, response=response,
                        msg=("The expiring policy is open-ended, so the new period cannot be "
                             "guessed. Give `period_from`, and close the old policy first."))
                period_from = old.period_to + timedelta(days=1)
            period_to = body.period_to
            if period_to is None and old.period_to is not None:
                period_to = date(period_from.year + 1, period_from.month, period_from.day) - timedelta(days=1)

            carried = {c.name for c in Policy.__table__.columns} - {
                "id", "created_at", "updated_at", "period_from", "period_to"}
            values = {name: getattr(old, name) for name in carried}
            values.update(body.overrides.model_dump(exclude_unset=True,
                                                    exclude={"insured", "reinsured", "retrocedent",
                                                             "period_from", "period_to",
                                                             "aircraft"}))
            new = Policy(period_from=period_from, period_to=period_to, **values)
            session.add(new)
            await session.flush()
            # the parties carry across in their order, unless an override names a role afresh
            errors: list[dict] = []
            sent = body.overrides.model_fields_set
            for role in _ROLES:
                refs = (getattr(body.overrides, role.value) or [] if role.value in sent
                        else [x.party_id for x in policy_parties(old, role)])
                await _write_parties(session, new.id, role, refs, [], errors)
            if errors:
                raise _ItemErrors([{**e, "loc": ("body", "overrides", *e["loc"][1:])}
                                   for e in errors])
            await session.flush()

            moved = 0
            if body.carry_aircraft:
                for cover in (await session.execute(
                    select(Coverage).where(Coverage.policy_id == policy_id)
                )).scalars().all():
                    session.add(Coverage(aircraft_id=cover.aircraft_id, policy_id=new.id,
                                         covered_from=period_from, covered_to=period_to))
                    moved += 1
                await session.flush()

            new = await reload_with(session, Policy, new.id, *_POLICY_LOAD)
            data = policy_json(new)
            data["aircraft_carried"] = moved
        await invalidate(request, PARTY)   # an override can name a new counterparty
        return success_response(
            request=request, response=response, data=data,
            msg=f"Renewed; {moved} aircraft carried onto the new policy",
            status_code=status.HTTP_201_CREATED)
    except IntegrityError as _ex:
        code, msg = integrity_error(_ex)
        return warning_response(request=request, response=response, msg=msg, status_code=code)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.delete(path="/policies/{policy_id}",
               description="Remove a policy raised in error. Refused with 409 while aircraft are "
                           "covered by it. A policy that simply expired is NOT deleted — it keeps "
                           "its period, and that is the history.",
               responses=build_responses(include=_OK))
async def delete_policy(request: Request, response: Response, policy_id: int,
                        token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        async with request.app.state.db_client.session(DB) as session:
            await set_actor(session, token)
            row = (await session.execute(
                select(Policy).where(Policy.id == policy_id).options(*_POLICY_LOAD)
            )).scalar_one_or_none()
            if row is None:
                return warning_response(request=request, response=response,
                                        msg=f"Policy {policy_id} not found",
                                        status_code=status.HTTP_404_NOT_FOUND)
            used = (await session.execute(
                select(func.count()).select_from(Coverage)
                .where(Coverage.policy_id == policy_id))).scalar_one()
            if used:
                return warning_response(
                    request=request, response=response,
                    msg=f"{used} aircraft are still covered by this policy — remove those first.",
                    status_code=status.HTTP_409_CONFLICT)
            data = policy_json(row)
            await session.delete(row)
        return success_response(request=request, response=response, data=data, msg="Policy deleted")
    except IntegrityError as _ex:
        code, msg = integrity_error(_ex)
        return warning_response(request=request, response=response, msg=msg, status_code=code)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


# ==============================================================================================
# coverage
# ==============================================================================================

@router.get(path="/coverage",
            description="Which aircraft are covered by which policy. `aircraft_id` / `policy_id` "
                        "narrow; `on_date` keeps only coverage in force that day. "
                        "Returns `{items, total}`.",
            responses=build_responses(include=_OK), dependencies=_READ)
async def list_coverage(request: Request, response: Response,
                        aircraft_id: Optional[int] = Query(None),
                        policy_id: Optional[int] = Query(None),
                        on_date: Optional[date] = Query(None),
                        limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                        sort: Optional[str] = Query(None), order: Optional[str] = Query(None)):
    try:
        conds = []
        if aircraft_id is not None:
            conds.append(Coverage.aircraft_id == aircraft_id)
        if policy_id is not None:
            conds.append(Coverage.policy_id == policy_id)
        if on_date is not None:
            conds.append(Coverage.covered_from <= on_date)
            conds.append((Coverage.covered_to.is_(None)) | (Coverage.covered_to >= on_date))
        stmt = apply_sort(
            select(Coverage, Aircraft).join(Aircraft, Aircraft.id == Coverage.aircraft_id)
            .where(*conds).options(*_COVERAGE_LOAD, *AIRCRAFT_BRIEF),
            sort=sort, order=order, sortmap=_COVERAGE_SORTS,
            tiebreak=(Coverage.covered_from.desc(), Coverage.id))
        async with request.app.state.db_client.read_session(DB) as session:
            rows, total = await page_with_total(
                session, stmt, limit=limit, offset=offset,
                count_stmt=select(func.count()).select_from(Coverage).where(*conds))
            data = {"items": [coverage_json(c, aircraft=a) for c, a in rows], "total": total}
        return success_response(request=request, response=response, data=data)
    except SortError as _ex:
        return warning_response(request=request, response=response, msg=str(_ex))
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.get(
    path="/coverage/compare",
    description=(
        "Required versus provided, per aircraft, on `on_date` (today by default): the three limits "
        "the lease stipulates beside the same three on the policy in force. `mismatches_only` "
        "keeps the rows where they disagree, or where one side is missing entirely — which is the "
        "report this two-sided schema exists to make possible."
    ),
    responses=build_responses(include=_OK), dependencies=_READ,
)
async def compare_cover(request: Request, response: Response,
                        on_date: Optional[date] = Query(None),
                        mismatches_only: bool = Query(False),
                        limit: int = Query(200, ge=1, le=1000), offset: int = Query(0, ge=0)):
    try:
        on = on_date or date.today()

        # The heaviest read in the domain: the WHOLE fleet, every lease in force and every
        # coverage in force, compared field by field in Python. It is also the one whose
        # answer changes least often, so it reads through the FLEET generation — which the
        # middleware bumps after any successful write under /fleet, /ref, /leasing or
        # /policy, i.e. after anything that could change the answer.
        async def load():
            async with request.app.state.db_client.read_session(DB) as session:
                # AIRCRAFT_BRIEF: the type and the airline that aircraft_json reads, joined into this
                # query, and the engines explicitly refused — they are rendered with engines=False and
                # asking for them later should fail loudly rather than quietly cost a query per row.
                aircraft = (await session.execute(
                    select(Aircraft).options(*AIRCRAFT_BRIEF)
                    .order_by(Aircraft.registration, Aircraft.id))).scalars().all()
                # ONE lease per aircraft — the newest not later than `on`, which is the only one this
                # comparison can use. Without DISTINCT ON this read EVERY lease ever recorded for
                # every aircraft and threw all but the last away in Python: a query bounded by the
                # HISTORY rather than by the fleet, and the history is the half that grows for ever.
                leases = (await session.execute(
                    select(AircraftLease).where(AircraftLease.effective_date <= on)
                    .options(joinedload(AircraftLease.agreement))
                    .distinct(AircraftLease.aircraft_id)
                    .order_by(AircraftLease.aircraft_id, AircraftLease.effective_date.desc(),
                              AircraftLease.id.desc())
                )).scalars().all()
                covers = (await session.execute(
                    select(Coverage).where(
                        Coverage.covered_from <= on,
                        (Coverage.covered_to.is_(None)) | (Coverage.covered_to >= on))
                    .options(joinedload(Coverage.policy))
                )).scalars().all()

                by_aircraft_lease: dict[int, list] = {}
                for l in leases:
                    by_aircraft_lease.setdefault(l.aircraft_id, []).append(l)
                by_aircraft_cover = {c.aircraft_id: c for c in covers}

                items = []
                for a in aircraft:
                    lease = lease_in_force(by_aircraft_lease.get(a.id, []), on)
                    cover = by_aircraft_cover.get(a.id)
                    policy = cover.policy if cover else None
                    fields = {}
                    agree = True
                    for column in COMPARED:
                        required = num(getattr(lease, column)) if lease else None
                        provided = num(getattr(policy, column)) if policy else None
                        ok = required == provided
                        agree = agree and ok
                        fields[column] = {"required": required, "provided": provided, "match": ok}
                    # `status` is shown, not consulted: it only restates whether a coverage covers
                    # today (revision service_status_sync), so it cannot excuse a missing policy.
                    service = a.service
                    row = {
                        "aircraft": aircraft_json(a, engines=False),
                        "has_lease": lease is not None,
                        "has_policy": policy is not None,
                        "policy_id": policy.id if policy else None,
                        "lease_id": lease.id if lease else None,
                        "status": enum_value(service.status) if service else "not_insured",
                        "usage_status": service.usage_status if service else None,
                        "match": agree and lease is not None and policy is not None,
                        "fields": fields,
                    }
                    if not mismatches_only or not row["match"]:
                        items.append(row)
                total = len(items)
                items = items[offset:offset + limit]
                data = {"items": items, "total": total, "as_of": on.isoformat()}
            return data

        data = await cached(request, FLEET,
                            {"grid": "compare", "on": on.isoformat(),
                             "mismatches_only": mismatches_only,
                             "limit": limit, "offset": offset}, load)
        return success_response(request=request, response=response, data=data)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.post(
    path="/coverage",
    description=(
        "Put one aircraft on a policy. `covered_from` / `covered_to` default to the policy period; "
        "set them for a mid-term delivery or redelivery. A 409 means the aircraft is already "
        "covered over part of that period — an aircraft holds one policy at a time."
    ),
    responses=build_responses(include=_OK | {status.HTTP_201_CREATED}),
)
async def create_coverage(request: Request, response: Response, body: CoverageIn,
                          token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        async with request.app.state.db_client.session(DB) as session:
            await set_actor(session, token)
            policy = await session.get(Policy, body.policy_id)
            if policy is None:
                return warning_response(request=request, response=response,
                                        msg=f"Policy {body.policy_id} not found",
                                        status_code=status.HTTP_404_NOT_FOUND)
            if body.aircraft_id is not None:
                aircraft = await session.get(Aircraft, body.aircraft_id, options=AIRCRAFT_BRIEF)
            else:
                aircraft = await find_aircraft(session, registration=body.registration,
                                               msn=body.msn, options=AIRCRAFT_BRIEF)
            if aircraft is None:
                return warning_response(
                    request=request, response=response,
                    msg="No such aircraft. Add the airframe at POST /fleet/aircraft first.",
                    status_code=status.HTTP_404_NOT_FOUND)

            row = Coverage(aircraft_id=aircraft.id, policy_id=policy.id,
                           covered_from=body.covered_from or policy.period_from,
                           covered_to=body.covered_to if body.covered_to is not None else policy.period_to)
            session.add(row)
            await session.flush()
            row = await reload_with(session, Coverage, row.id, *_COVERAGE_LOAD)
            data = coverage_json(row, aircraft=aircraft)
        return success_response(request=request, response=response, data=data,
                                status_code=status.HTTP_201_CREATED)
    except IntegrityError as _ex:
        code, msg = integrity_error(_ex)
        return warning_response(request=request, response=response, msg=msg, status_code=code)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.post(
    path="/coverage/bulk",
    description=(
        "Put many aircraft on one policy in ONE request: up to 1000 items, each `aircraft_id` or "
        "`registration` / `msn`, with an optional window (defaults to the policy period). All are "
        "saved or none. An aircraft already covered over part of its window is a 409 naming every "
        "such aircraft; an item that matches no aircraft, or repeats another, is a 422 per item "
        "(`body.aircraft.<index>.<field>`). Returns the policy with all its aircraft."
    ),
    status_code=status.HTTP_201_CREATED,
    responses=build_responses(include=_OK | {status.HTTP_201_CREATED}),
)
async def create_coverage_bulk(request: Request, response: Response, body: CoverageBulkIn,
                               token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        async with request.app.state.db_client.session(DB) as session:
            await set_actor(session, token)
            policy = (await session.execute(
                select(Policy).where(Policy.id == body.policy_id))).scalar_one_or_none()
            if policy is None:
                return warning_response(request=request, response=response,
                                        msg=f"Policy {body.policy_id} not found",
                                        status_code=status.HTTP_404_NOT_FOUND)
            resolved = await _resolve_items(session, policy, body.aircraft, ("body", "aircraft"))
            added = await _insert_coverages(session, policy, resolved)
            data = await _policy_detail(session, policy.id)
        return success_response(request=request, response=response, data=data,
                                msg=f"{added} aircraft added to the policy",
                                status_code=status.HTTP_201_CREATED)
    except _WRITE_ERRORS as _ex:
        return _write_failed(request, response, _ex)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.patch(path="/coverage/{coverage_id}",
              description="Change when a policy covers an aircraft — a redelivery closes the "
                          "window with `covered_to`. A 409 means the new window overlaps another "
                          "policy on the same airframe.",
              responses=build_responses(include=_OK))
async def update_coverage(request: Request, response: Response, coverage_id: int,
                          body: CoveragePatch,
                          token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        fields = body.model_dump(exclude_unset=True)
        async with request.app.state.db_client.session(DB) as session:
            await set_actor(session, token)
            row = (await session.execute(
                select(Coverage).where(Coverage.id == coverage_id).options(*_COVERAGE_LOAD)
            )).scalar_one_or_none()
            if row is None:
                return warning_response(request=request, response=response,
                                        msg=f"Coverage {coverage_id} not found",
                                        status_code=status.HTTP_404_NOT_FOUND)
            for key, value in fields.items():
                setattr(row, key, value)
            await session.flush()
            aircraft = await session.get(Aircraft, row.aircraft_id, options=AIRCRAFT_BRIEF)
            data = coverage_json(row, aircraft=aircraft)
        return success_response(request=request, response=response, data=data)
    except IntegrityError as _ex:
        code, msg = integrity_error(_ex)
        return warning_response(request=request, response=response, msg=msg, status_code=code)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.delete(path="/coverage/{coverage_id}",
               description="Take an aircraft off a policy it was never on. Coverage that simply "
                           "ended is closed with `covered_to`, not deleted.",
               responses=build_responses(include=_OK))
async def delete_coverage(request: Request, response: Response, coverage_id: int,
                          token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        async with request.app.state.db_client.session(DB) as session:
            await set_actor(session, token)
            row = (await session.execute(
                select(Coverage).where(Coverage.id == coverage_id).options(*_COVERAGE_LOAD)
            )).scalar_one_or_none()
            if row is None:
                return warning_response(request=request, response=response,
                                        msg=f"Coverage {coverage_id} not found",
                                        status_code=status.HTTP_404_NOT_FOUND)
            aircraft = await session.get(Aircraft, row.aircraft_id, options=AIRCRAFT_BRIEF)
            data = coverage_json(row, aircraft=aircraft)
            await session.delete(row)
        return success_response(request=request, response=response, data=data,
                                msg="Coverage deleted")
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)
