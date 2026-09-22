"""Insurance policies and per-aircraft coverage: `/policy/policies`, `/policy/coverage`.

This side holds what the insurer PROVIDES. Its twin is `/leasing`, which holds what the lease
REQUIRES; `GET /policy/coverage/compare` puts the two side by side, which is the question the whole
split exists to answer.

A POLICY COVERS A FLEET. Every limit and deductible lives on the policy, once, because that is how
it is written; `policy.coverage` says which airframes it covers and over what window. An aircraft
holds ONE policy at a time — a second overlapping coverage is refused by the database, not by a
check here.

RENEWAL IS THE NORMAL CASE. `POST /policy/policies/{id}/renew` creates next year's policy with the
same terms and carries the chosen aircraft onto it, leaving the expiring policy and its coverage
rows untouched. That is what makes an aircraft's insurance history readable years later: a chain of
coverage rows, each pointing at the contract that was in force.
"""
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from fastapi import Request, Response, Depends, Query, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from Config import setup_logger
from settings import Router
from Database import ApiToken
from Database.RefModels import Party
from Database.FleetModels import Aircraft
from Database.LeasingModels import AircraftLease
from Database.FleetModels import InsuranceStatus
from Database.PolicyModels import Policy, Coverage
from api_auth import authorize, SCOPE_INSURANCE_READ, SCOPE_INSURANCE_WRITE
from Utils import success_response, warning_response, error_response
from Utils.ResponsesFunc import build_responses
from Utils.DomainCache import PARTY, invalidate
from Utils.DomainCommon import (
    DB, set_actor, apply_sort, SortError, integrity_error, find_aircraft, get_or_create_party,
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

_POLICY_LOAD = (selectinload(Policy.insured), selectinload(Policy.reinsured),
                selectinload(Policy.retrocedent))
_COVERAGE_LOAD = (selectinload(Coverage.policy).selectinload(Policy.insured),
                  selectinload(Coverage.policy).selectinload(Policy.reinsured),
                  selectinload(Coverage.policy).selectinload(Policy.retrocedent))

# The three columns that exist on BOTH the lease and the policy, so required and provided cover can
# be compared. Kept in one place so /coverage/compare and the docs cannot drift apart.
COMPARED = ("combined_single_limit", "hull_spares_war_excess_liability", "hull_deductible_buy_down")


# ==============================================================================================
# bodies
# ==============================================================================================

class PolicyIn(BaseModel):
    """Parties are NAMES, found or created in ref.party — the insured on an aviation policy is
    routinely a lessor or a holding company rather than the operating airline."""
    insured: Optional[str] = Field(default=None, max_length=256)
    insured_id: Optional[int] = None
    reinsured: Optional[str] = Field(default=None, max_length=256)
    retrocedent: Optional[str] = Field(default=None, max_length=256)

    period_from: date
    period_to: Optional[date] = None

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
        default=None, ge=0, le=100, description="PERCENT of the risk ceded (97.5 = 97.5 %).")
    cut_through_clause: Optional[str] = None

    @model_validator(mode="after")
    def _check(self):
        if self.insured_id is None and not self.insured:
            raise ValueError("give `insured` (a name) or `insured_id`")
        if self.period_to is not None and self.period_to < self.period_from:
            raise ValueError("`period_to` must not be earlier than `period_from`")
        return self


class PolicyPatch(BaseModel):
    insured: Optional[str] = Field(default=None, max_length=256)
    reinsured: Optional[str] = Field(default=None, max_length=256)
    retrocedent: Optional[str] = Field(default=None, max_length=256)
    period_from: Optional[date] = None
    period_to: Optional[date] = None
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
    selected_country: Optional[str] = Field(default=None, max_length=128)
    reinsured_amount: Optional[Decimal] = Field(default=None, ge=0, le=100)
    cut_through_clause: Optional[str] = None


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
    overrides: PolicyPatch = Field(default_factory=PolicyPatch)


# ==============================================================================================
# policies
# ==============================================================================================

@router.get(
    path="/policies",
    description=(
        "Insurance policies. `insured_id` narrows to one party; `on_date` keeps only the policies "
        "in force that day; `active` is shorthand for on_date=today. Returns `{items, total}`."
    ),
    responses=build_responses(include=_OK), dependencies=_READ,
)
async def list_policies(request: Request, response: Response,
                        insured_id: Optional[int] = Query(None),
                        on_date: Optional[date] = Query(None),
                        active: bool = Query(False, description="Shorthand for on_date=today."),
                        limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                        sort: Optional[str] = Query(None), order: Optional[str] = Query(None)):
    try:
        conds = []
        if insured_id is not None:
            conds.append(Policy.insured_id == insured_id)
        on = on_date or (date.today() if active else None)
        if on is not None:
            conds.append(Policy.period_from <= on)
            conds.append((Policy.period_to.is_(None)) | (Policy.period_to >= on))
        stmt = apply_sort(select(Policy).where(*conds).options(*_POLICY_LOAD),
                          sort=sort, order=order, sortmap=_POLICY_SORTS,
                          tiebreak=(Policy.period_from.desc(), Policy.id))
        async with request.app.state.db_client.read_session(DB) as session:
            total = (await session.execute(
                select(func.count()).select_from(Policy).where(*conds))).scalar_one()
            rows = (await session.execute(stmt.limit(limit).offset(offset))).scalars().all()
            data = {"items": [policy_json(p) for p in rows], "total": total}
        return success_response(request=request, response=response, data=data)
    except SortError as _ex:
        return warning_response(request=request, response=response, msg=str(_ex))
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.post(
    path="/policies",
    description=(
        "Add a policy. Identified by insured and period — sending the same pair twice is a 409, "
        "not a duplicate. `reinsured_amount` is a percent. Put the aircraft on it afterwards with "
        "POST /policy/coverage, or renew an existing policy to carry them across."
    ),
    responses=build_responses(include=_OK | {status.HTTP_201_CREATED}),
)
async def create_policy(request: Request, response: Response, body: PolicyIn,
                        token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        async with request.app.state.db_client.session(DB) as session:
            await set_actor(session, token)
            if body.insured_id is not None:
                insured = await session.get(Party, body.insured_id)
                if insured is None:
                    return warning_response(request=request, response=response,
                                            msg=f"Party {body.insured_id} not found",
                                            status_code=status.HTTP_404_NOT_FOUND)
            else:
                insured = await get_or_create_party(session, body.insured)
            reinsured = await get_or_create_party(session, body.reinsured)
            retrocedent = await get_or_create_party(session, body.retrocedent)

            fields = body.model_dump(exclude={"insured", "insured_id", "reinsured", "retrocedent"})
            row = Policy(insured_id=insured.id,
                         reinsured_id=reinsured.id if reinsured else None,
                         retrocedent_id=retrocedent.id if retrocedent else None, **fields)
            session.add(row)
            await session.flush()
            await session.refresh(row, ["insured", "reinsured", "retrocedent"])
            data = policy_json(row)
        await invalidate(request, PARTY)   # a counterparty may have been created
        return success_response(request=request, response=response, data=data,
                                status_code=status.HTTP_201_CREATED)
    except IntegrityError as _ex:
        code, msg = integrity_error(_ex)
        return warning_response(request=request, response=response, msg=msg, status_code=code)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.get(path="/policies/{policy_id}",
            description="One policy with every aircraft covered by it.",
            responses=build_responses(include=_OK), dependencies=_READ)
async def get_policy(request: Request, response: Response, policy_id: int):
    try:
        async with request.app.state.db_client.read_session(DB) as session:
            row = (await session.execute(
                select(Policy).where(Policy.id == policy_id).options(*_POLICY_LOAD)
            )).scalar_one_or_none()
            if row is None:
                return warning_response(request=request, response=response,
                                        msg=f"Policy {policy_id} not found",
                                        status_code=status.HTTP_404_NOT_FOUND)
            covered = (await session.execute(
                select(Coverage, Aircraft).join(Aircraft, Aircraft.id == Coverage.aircraft_id)
                .where(Coverage.policy_id == policy_id)
                .order_by(Aircraft.registration)
            )).all()
            data = policy_json(row)
            data["aircraft"] = [
                {"coverage_id": c.id, "covered_from": c.covered_from.isoformat(),
                 "covered_to": c.covered_to.isoformat() if c.covered_to else None,
                 "aircraft": aircraft_json(a, engines=False)}
                for c, a in covered
            ]
        return success_response(request=request, response=response, data=data)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.patch(path="/policies/{policy_id}",
              description="Endorse or correct a policy. Only the fields sent are touched and the "
                          "previous values stay in the audit log. A RENEWAL is not a patch — use "
                          "/renew, so the expiring contract keeps its own terms.",
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
            for name, column in (("insured", "insured_id"), ("reinsured", "reinsured_id"),
                                 ("retrocedent", "retrocedent_id")):
                if name in fields:
                    party = await get_or_create_party(session, fields.pop(name))
                    setattr(row, column, party.id if party else None)
            for key, value in fields.items():
                setattr(row, key, value)
            await session.flush()
            # `updated_at` is computed by the database on UPDATE, so SQLAlchemy expires it after
            # the flush. Read it here, inside the session, or serializing the row later triggers
            # lazy IO outside the greenlet context and the request 500s.
            await session.refresh(row, ["insured", "reinsured", "retrocedent", "updated_at"])
            data = policy_json(row)
        await invalidate(request, PARTY)   # a counterparty may have been created
        return success_response(request=request, response=response, data=data)
    except IntegrityError as _ex:
        code, msg = integrity_error(_ex)
        return warning_response(request=request, response=response, msg=msg, status_code=code)
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
                                                             "period_from", "period_to"}))
            for name, column in (("insured", "insured_id"), ("reinsured", "reinsured_id"),
                                 ("retrocedent", "retrocedent_id")):
                override = getattr(body.overrides, name, None)
                if override:
                    party = await get_or_create_party(session, override)
                    values[column] = party.id if party else None
            new = Policy(period_from=period_from, period_to=period_to, **values)
            session.add(new)
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

            await session.refresh(new, ["insured", "reinsured", "retrocedent"])
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
            .where(*conds).options(*_COVERAGE_LOAD),
            sort=sort, order=order, sortmap=_COVERAGE_SORTS,
            tiebreak=(Coverage.covered_from.desc(), Coverage.id))
        async with request.app.state.db_client.read_session(DB) as session:
            total = (await session.execute(
                select(func.count()).select_from(Coverage).where(*conds))).scalar_one()
            rows = (await session.execute(stmt.limit(limit).offset(offset))).all()
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
        async with request.app.state.db_client.read_session(DB) as session:
            aircraft = (await session.execute(
                select(Aircraft).options(selectinload(Aircraft.service))
                .order_by(Aircraft.registration, Aircraft.id))).scalars().all()
            leases = (await session.execute(
                select(AircraftLease).where(AircraftLease.effective_date <= on)
                .options(selectinload(AircraftLease.agreement))
            )).scalars().all()
            covers = (await session.execute(
                select(Coverage).where(
                    Coverage.covered_from <= on,
                    (Coverage.covered_to.is_(None)) | (Coverage.covered_to >= on))
                .options(selectinload(Coverage.policy))
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
                # An aircraft whose service block STATES it is uncovered is not a missing policy.
                # It is an answer, so it counts as a match and carries the reason, not a red flag.
                service = a.service
                declared_uncovered = (
                    service is not None
                    and enum_value(service.status) == InsuranceStatus.NOT_INSURED.value)
                row = {
                    "aircraft": aircraft_json(a, engines=False),
                    "has_lease": lease is not None,
                    "has_policy": policy is not None,
                    "policy_id": policy.id if policy else None,
                    "lease_id": lease.id if lease else None,
                    "status": enum_value(service.status) if service else "insured",
                    "usage_status": service.usage_status if service else None,
                    "match": declared_uncovered or (
                        agree and lease is not None and policy is not None),
                    "fields": fields,
                }
                if not mismatches_only or not row["match"]:
                    items.append(row)
            total = len(items)
            items = items[offset:offset + limit]
            data = {"items": items, "total": total, "as_of": on.isoformat()}
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
                aircraft = await session.get(Aircraft, body.aircraft_id)
            else:
                aircraft = await find_aircraft(session, registration=body.registration, msn=body.msn)
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
            await session.refresh(row, ["policy"])
            await session.refresh(policy, ["insured", "reinsured", "retrocedent"])
            data = coverage_json(row, aircraft=aircraft)
        return success_response(request=request, response=response, data=data,
                                status_code=status.HTTP_201_CREATED)
    except IntegrityError as _ex:
        code, msg = integrity_error(_ex)
        return warning_response(request=request, response=response, msg=msg, status_code=code)
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
            aircraft = await session.get(Aircraft, row.aircraft_id)
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
            aircraft = await session.get(Aircraft, row.aircraft_id)
            data = coverage_json(row, aircraft=aircraft)
            await session.delete(row)
        return success_response(request=request, response=response, data=data,
                                msg="Coverage deleted")
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)
