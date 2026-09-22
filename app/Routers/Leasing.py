"""Lease agreements and their per-aircraft terms: `/leasing/agreements`, `/leasing/leases`.

This side of the domain holds what the LESSOR REQUIRES. Its twin is `/policy`, which holds what
the insurer actually PROVIDES — `combined_single_limit`, `hull_spares_war_excess_liability` and
`hull_deductible_buy_down` exist on both so the two can be compared. They are equal most of the
time, and the times they are not are the point.

A LEASE ROW HAS NO END DATE. A change of terms is a new row with a later `effective_date`; the
terms in force on a day are the newest row not later than it. So `GET /leasing/leases` defaults to
the terms in force TODAY (`current_only=true`) and `current_only=false` shows the whole sequence.
PATCH is for correcting a row that was written wrongly — not for a renegotiation, which is a POST.

THE SERVICE BLOCK IS NOT HERE. `source`, `status`, `usage_status`, `agreed_value_fixed` and the
currency moved to `fleet.service_info`, one row per aircraft, and are read and written at
`/fleet/aircraft/{id}/service`. A lease payload still SHOWS the currency and the depreciated value,
both resolved through that row.

AGREED VALUE. `agreed_value_final` is what the schedule STATES. `agreed_value_calculated` is the
same figure derived from `agreed_value_preliminary`, `depreciation_ratio` and
`depreciation_start_date` — compounding on whole years — as of `on_date`. Both are returned and
neither overwrites the other; a difference between them is information, not an error.
"""
from datetime import date
from decimal import Decimal
from typing import Optional

from fastapi import Request, Response, Depends, Query, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from Config import setup_logger
from settings import Router
from Database import ApiToken
from Database.RefModels import Party
from Database.FleetModels import Aircraft
from Database.LeasingModels import Agreement, AircraftLease
from api_auth import authorize, SCOPE_INSURANCE_READ, SCOPE_INSURANCE_WRITE
from Utils import success_response, warning_response, error_response
from Utils.ResponsesFunc import build_responses
from Utils.DomainCache import PARTY, invalidate
from Utils.DomainCommon import (
    DB, norm, set_actor, apply_sort, SortError, integrity_error, find_aircraft,
    get_or_create_party, agreement_json, lease_json,
)

logger = setup_logger("leasing_api")

router = Router(prefix="/leasing", tags=["Leasing"])

_AGREEMENT_SORTS = {
    "name": Agreement.name,
    "start_date": Agreement.start_date,
    "created_at": Agreement.created_at,
}
_LEASE_SORTS = {
    "effective_date": AircraftLease.effective_date,
    "agreed_value_preliminary": AircraftLease.agreed_value_preliminary,
    "agreed_value_final": AircraftLease.agreed_value_final,
    "depreciation_ratio": AircraftLease.depreciation_ratio,
    "combined_single_limit": AircraftLease.combined_single_limit,
    "hull_deductible_buy_down": AircraftLease.hull_deductible_buy_down,
    "created_at": AircraftLease.created_at,
    "updated_at": AircraftLease.updated_at,
}

_READ = [Depends(authorize(SCOPE_INSURANCE_READ))]
_OK = {status.HTTP_200_OK, status.HTTP_400_BAD_REQUEST, status.HTTP_404_NOT_FOUND,
       status.HTTP_409_CONFLICT, status.HTTP_500_INTERNAL_SERVER_ERROR}

# agreement -> lessor is to-one twice over: one JOIN, not two extra round trips
_LEASE_LOAD = (joinedload(AircraftLease.agreement).joinedload(Agreement.lessor),)


# ==============================================================================================
# bodies
# ==============================================================================================

class AgreementIn(BaseModel):
    name: str = Field(min_length=1, max_length=512)
    start_date: Optional[date] = None
    lessor: Optional[str] = Field(default=None, max_length=256,
                                  description="Counterparty NAME — found or created in ref.party.")
    alternative_contract_party: Optional[str] = None
    other_contracts: Optional[str] = Field(
        default=None, description="Contracts to be mentioned other than the lease agreement.")


class AgreementPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=512)
    start_date: Optional[date] = None
    lessor: Optional[str] = Field(default=None, max_length=256)
    alternative_contract_party: Optional[str] = None
    other_contracts: Optional[str] = None


class LeaseIn(BaseModel):
    """The aircraft and the agreement are given by NAME or by id — whichever the caller has.
    An unknown agreement is created; an unknown aircraft is NOT, because an airframe entered by
    accident here would be invisible until someone noticed a duplicate MSN."""
    aircraft_id: Optional[int] = None
    registration: Optional[str] = Field(default=None, max_length=32)
    msn: Optional[str] = Field(default=None, max_length=64)

    agreement_id: Optional[int] = None
    agreement_name: Optional[str] = Field(default=None, max_length=512)
    agreement_start_date: Optional[date] = None
    lessor: Optional[str] = Field(default=None, max_length=256)

    effective_date: date
    agreed_value_preliminary: Optional[Decimal] = Field(default=None, ge=0)
    agreed_value_final: Optional[Decimal] = Field(default=None, ge=0)
    depreciation_ratio: Optional[Decimal] = Field(
        default=None, ge=0, le=100, description="PERCENT per annum (5 = 5 %/year), not a fraction.")
    depreciation_start_date: Optional[date] = None
    combined_single_limit: Optional[Decimal] = Field(default=None, ge=0)
    hull_spares_war_excess_liability: Optional[Decimal] = Field(default=None, ge=0)
    hull_deductible_buy_down: Optional[Decimal] = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _check(self):
        if self.aircraft_id is None and not (self.registration or self.msn):
            raise ValueError("give `aircraft_id`, or `registration` / `msn` to look one up")
        if self.agreement_id is None and not self.agreement_name:
            raise ValueError("give `agreement_id`, or `agreement_name` to find or create one")
        return self


class LeasePatch(BaseModel):
    effective_date: Optional[date] = None
    agreed_value_preliminary: Optional[Decimal] = Field(default=None, ge=0)
    agreed_value_final: Optional[Decimal] = Field(default=None, ge=0)
    depreciation_ratio: Optional[Decimal] = Field(default=None, ge=0, le=100)
    depreciation_start_date: Optional[date] = None
    combined_single_limit: Optional[Decimal] = Field(default=None, ge=0)
    hull_spares_war_excess_liability: Optional[Decimal] = Field(default=None, ge=0)
    hull_deductible_buy_down: Optional[Decimal] = Field(default=None, ge=0)


# ==============================================================================================
# agreements
# ==============================================================================================

@router.get(path="/agreements",
            description="Lease agreements. `q` matches the name or the lessor. `lessor_id` narrows "
                        "to one counterparty. Returns `{items, total}`.",
            responses=build_responses(include=_OK), dependencies=_READ)
async def list_agreements(request: Request, response: Response,
                          q: str = Query(""), lessor_id: Optional[int] = Query(None),
                          limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                          sort: Optional[str] = Query(None), order: Optional[str] = Query(None)):
    try:
        conds = []
        q = q.strip()
        if q:
            conds.append(or_(
                Agreement.name.ilike(f"%{q}%"),
                Agreement.lessor_id.in_(select(Party.id).where(Party.name.ilike(f"%{q}%"))),
            ))
        if lessor_id is not None:
            conds.append(Agreement.lessor_id == lessor_id)
        stmt = apply_sort(select(Agreement).where(*conds).options(joinedload(Agreement.lessor)),
                          sort=sort, order=order, sortmap=_AGREEMENT_SORTS,
                          tiebreak=(Agreement.name, Agreement.id))
        async with request.app.state.db_client.read_session(DB) as session:
            total = (await session.execute(
                select(func.count()).select_from(Agreement).where(*conds))).scalar_one()
            rows = (await session.execute(stmt.limit(limit).offset(offset))).scalars().all()
            data = {"items": [agreement_json(g) for g in rows], "total": total}
        return success_response(request=request, response=response, data=data)
    except SortError as _ex:
        return warning_response(request=request, response=response, msg=str(_ex))
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.post(path="/agreements",
             description="Add a lease agreement. Identified by name and start date — sending the "
                         "same pair twice is a 409, not a duplicate. `lessor` is a name and is "
                         "found or created in ref.party.",
             responses=build_responses(include=_OK | {status.HTTP_201_CREATED}))
async def create_agreement(request: Request, response: Response, body: AgreementIn,
                           token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        async with request.app.state.db_client.session(DB) as session:
            await set_actor(session, token)
            lessor = await get_or_create_party(session, body.lessor)
            row = Agreement(
                name=body.name.strip(), start_date=body.start_date,
                lessor_id=lessor.id if lessor else None,
                alternative_contract_party=body.alternative_contract_party,
                other_contracts=body.other_contracts,
            )
            session.add(row)
            await session.flush()
            await session.refresh(row, ["lessor"])
            data = agreement_json(row)
        await invalidate(request, PARTY)   # a counterparty may have been created
        return success_response(request=request, response=response, data=data,
                                status_code=status.HTTP_201_CREATED)
    except IntegrityError as _ex:
        code, msg = integrity_error(_ex)
        return warning_response(request=request, response=response, msg=msg, status_code=code)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.get(path="/agreements/{agreement_id}",
            description="One lease agreement with every aircraft on it.",
            responses=build_responses(include=_OK), dependencies=_READ)
async def get_agreement(request: Request, response: Response, agreement_id: int,
                        on_date: Optional[date] = Query(None)):
    try:
        on = on_date or date.today()
        async with request.app.state.db_client.read_session(DB) as session:
            row = (await session.execute(
                select(Agreement).where(Agreement.id == agreement_id)
                .options(joinedload(Agreement.lessor))
            )).scalar_one_or_none()
            if row is None:
                return warning_response(request=request, response=response,
                                        msg=f"Agreement {agreement_id} not found",
                                        status_code=status.HTTP_404_NOT_FOUND)
            leases = (await session.execute(
                select(AircraftLease, Aircraft)
                .join(Aircraft, Aircraft.id == AircraftLease.aircraft_id)
                .where(AircraftLease.agreement_id == agreement_id)
                .options(*_LEASE_LOAD)
                .order_by(Aircraft.registration, AircraftLease.effective_date.desc())
            )).all()
            data = agreement_json(row)
            data["aircraft"] = [lease_json(l, on=on, aircraft=a) for l, a in leases]
        return success_response(request=request, response=response, data=data)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.patch(path="/agreements/{agreement_id}", description="Correct a lease agreement.",
              responses=build_responses(include=_OK))
async def update_agreement(request: Request, response: Response, agreement_id: int,
                           body: AgreementPatch,
                           token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        fields = body.model_dump(exclude_unset=True)
        async with request.app.state.db_client.session(DB) as session:
            await set_actor(session, token)
            row = (await session.execute(
                select(Agreement).where(Agreement.id == agreement_id)
                .options(joinedload(Agreement.lessor))
            )).scalar_one_or_none()
            if row is None:
                return warning_response(request=request, response=response,
                                        msg=f"Agreement {agreement_id} not found",
                                        status_code=status.HTTP_404_NOT_FOUND)
            if "lessor" in fields:
                lessor = await get_or_create_party(session, fields.pop("lessor"))
                row.lessor_id = lessor.id if lessor else None
            for key, value in fields.items():
                setattr(row, key, value.strip() if key == "name" and value else value)
            await session.flush()
            await session.refresh(row, ["lessor"])
            data = agreement_json(row)
        await invalidate(request, PARTY)   # a counterparty may have been created
        return success_response(request=request, response=response, data=data)
    except IntegrityError as _ex:
        code, msg = integrity_error(_ex)
        return warning_response(request=request, response=response, msg=msg, status_code=code)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.delete(path="/agreements/{agreement_id}",
               description="Remove a lease agreement. Refused with 409 while aircraft are still "
                           "on it — remove their lease records first.",
               responses=build_responses(include=_OK))
async def delete_agreement(request: Request, response: Response, agreement_id: int,
                           token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        async with request.app.state.db_client.session(DB) as session:
            await set_actor(session, token)
            row = (await session.execute(
                select(Agreement).where(Agreement.id == agreement_id)
                .options(joinedload(Agreement.lessor))
            )).scalar_one_or_none()
            if row is None:
                return warning_response(request=request, response=response,
                                        msg=f"Agreement {agreement_id} not found",
                                        status_code=status.HTTP_404_NOT_FOUND)
            used = (await session.execute(
                select(func.count()).select_from(AircraftLease)
                .where(AircraftLease.agreement_id == agreement_id))).scalar_one()
            if used:
                return warning_response(
                    request=request, response=response,
                    msg=f"{used} aircraft are still on this agreement — remove those first.",
                    status_code=status.HTTP_409_CONFLICT)
            data = agreement_json(row)
            await session.delete(row)
        return success_response(request=request, response=response, data=data,
                                msg="Agreement deleted")
    except IntegrityError as _ex:
        code, msg = integrity_error(_ex)
        return warning_response(request=request, response=response, msg=msg, status_code=code)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


# ==============================================================================================
# per-aircraft lease terms
# ==============================================================================================

@router.get(
    path="/leases",
    description=(
        "Per-aircraft lease terms. By default returns the terms IN FORCE on `on_date` (today) — "
        "one row per aircraft. Pass `current_only=false` for the whole sequence, which is how a "
        "change of terms is recorded. `agreed_value_calculated` is the depreciated value at "
        "`on_date` beside the stated one. Returns `{items, total}`."
    ),
    responses=build_responses(include=_OK), dependencies=_READ,
)
async def list_leases(
    request: Request, response: Response,
    aircraft_id: Optional[int] = Query(None), agreement_id: Optional[int] = Query(None),
    on_date: Optional[date] = Query(None, description="Which day the terms are read for."),
    current_only: bool = Query(True, description="One row per aircraft, in force on on_date."),
    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
    sort: Optional[str] = Query(None), order: Optional[str] = Query(None),
):
    try:
        on = on_date or date.today()
        conds = []
        if aircraft_id is not None:
            conds.append(AircraftLease.aircraft_id == aircraft_id)
        if agreement_id is not None:
            conds.append(AircraftLease.agreement_id == agreement_id)
        if current_only:
            conds.append(AircraftLease.effective_date <= on)

        async with request.app.state.db_client.read_session(DB) as session:
            stmt = (select(AircraftLease, Aircraft)
                    .join(Aircraft, Aircraft.id == AircraftLease.aircraft_id)
                    .where(*conds).options(*_LEASE_LOAD))
            if current_only:
                # one per aircraft: the newest row not later than `on`. DISTINCT ON needs the
                # distinct column to lead ORDER BY, so paging and sorting happen in Python below.
                stmt = stmt.distinct(AircraftLease.aircraft_id).order_by(
                    AircraftLease.aircraft_id, AircraftLease.effective_date.desc(),
                    AircraftLease.id.desc())
                rows = (await session.execute(stmt)).all()
                rows.sort(key=lambda r: (r[1].registration, r[0].id))
                total = len(rows)
                rows = rows[offset:offset + limit]
            else:
                stmt = apply_sort(stmt, sort=sort, order=order, sortmap=_LEASE_SORTS,
                                  tiebreak=(AircraftLease.effective_date.desc(), AircraftLease.id))
                total = (await session.execute(
                    select(func.count()).select_from(AircraftLease).where(*conds))).scalar_one()
                rows = (await session.execute(stmt.limit(limit).offset(offset))).all()
            data = {"items": [lease_json(l, on=on, aircraft=a) for l, a in rows], "total": total}
        return success_response(request=request, response=response, data=data)
    except SortError as _ex:
        return warning_response(request=request, response=response, msg=str(_ex))
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.post(
    path="/leases",
    description=(
        "Record the lease terms for one aircraft from a given effective date. A RENEGOTIATION is "
        "another POST with a later `effective_date` — the earlier row keeps its own period, which "
        "is the business history. The agreement is found or created; the aircraft must already "
        "exist (add it at /fleet/aircraft first). `depreciation_ratio` is a percent."
    ),
    responses=build_responses(include=_OK | {status.HTTP_201_CREATED}),
)
async def create_lease(request: Request, response: Response, body: LeaseIn,
                       token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        async with request.app.state.db_client.session(DB) as session:
            await set_actor(session, token)

            if body.aircraft_id is not None:
                aircraft = await session.get(Aircraft, body.aircraft_id)
            else:
                aircraft = await find_aircraft(session, registration=body.registration, msn=body.msn)
            if aircraft is None:
                return warning_response(
                    request=request, response=response,
                    msg="No such aircraft. Add the airframe at POST /fleet/aircraft first.",
                    status_code=status.HTTP_404_NOT_FOUND)

            if body.agreement_id is not None:
                agreement = await session.get(Agreement, body.agreement_id)
                if agreement is None:
                    return warning_response(request=request, response=response,
                                            msg=f"Agreement {body.agreement_id} not found",
                                            status_code=status.HTTP_404_NOT_FOUND)
            else:
                agreement = (await session.execute(
                    select(Agreement).where(
                        Agreement.name_normalized == norm(body.agreement_name),
                        Agreement.start_date.is_(body.agreement_start_date)
                        if body.agreement_start_date is None
                        else Agreement.start_date == body.agreement_start_date)
                )).scalar_one_or_none()
                if agreement is None:
                    lessor = await get_or_create_party(session, body.lessor)
                    agreement = Agreement(
                        name=body.agreement_name.strip(), start_date=body.agreement_start_date,
                        lessor_id=lessor.id if lessor else None)
                    session.add(agreement)
                    await session.flush()

            row = AircraftLease(
                aircraft_id=aircraft.id, agreement_id=agreement.id,
                effective_date=body.effective_date,
                agreed_value_preliminary=body.agreed_value_preliminary,
                agreed_value_final=body.agreed_value_final,
                depreciation_ratio=body.depreciation_ratio,
                depreciation_start_date=body.depreciation_start_date,
                combined_single_limit=body.combined_single_limit,
                hull_spares_war_excess_liability=body.hull_spares_war_excess_liability,
                hull_deductible_buy_down=body.hull_deductible_buy_down,
            )
            session.add(row)
            await session.flush()
            await session.refresh(row, ["agreement"])
            await session.refresh(agreement, ["lessor"])
            data = lease_json(row, aircraft=aircraft)
        await invalidate(request, PARTY)   # a counterparty may have been created
        return success_response(request=request, response=response, data=data,
                                status_code=status.HTTP_201_CREATED)
    except IntegrityError as _ex:
        code, msg = integrity_error(_ex)
        return warning_response(request=request, response=response, msg=msg, status_code=code)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.get(path="/leases/{lease_id}", description="One lease record.",
            responses=build_responses(include=_OK), dependencies=_READ)
async def get_lease(request: Request, response: Response, lease_id: int,
                    on_date: Optional[date] = Query(None)):
    try:
        on = on_date or date.today()
        async with request.app.state.db_client.read_session(DB) as session:
            row = (await session.execute(
                select(AircraftLease).where(AircraftLease.id == lease_id).options(*_LEASE_LOAD)
            )).scalar_one_or_none()
            if row is None:
                return warning_response(request=request, response=response,
                                        msg=f"Lease {lease_id} not found",
                                        status_code=status.HTTP_404_NOT_FOUND)
            aircraft = await session.get(Aircraft, row.aircraft_id)
            data = lease_json(row, on=on, aircraft=aircraft)
        return success_response(request=request, response=response, data=data)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.patch(
    path="/leases/{lease_id}",
    description=(
        "Correct a lease record. This is for a row that was written wrongly — a RENEGOTIATION is "
        "a new record with a later effective date, so that the old terms keep their period. The "
        "previous values stay in the audit log either way."
    ),
    responses=build_responses(include=_OK),
)
async def update_lease(request: Request, response: Response, lease_id: int, body: LeasePatch,
                       token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        fields = body.model_dump(exclude_unset=True)
        async with request.app.state.db_client.session(DB) as session:
            await set_actor(session, token)
            row = (await session.execute(
                select(AircraftLease).where(AircraftLease.id == lease_id).options(*_LEASE_LOAD)
            )).scalar_one_or_none()
            if row is None:
                return warning_response(request=request, response=response,
                                        msg=f"Lease {lease_id} not found",
                                        status_code=status.HTTP_404_NOT_FOUND)
            for key, value in fields.items():
                setattr(row, key, value)
            await session.flush()
            # `updated_at` is computed by the database on UPDATE, so SQLAlchemy expires it
            # after the flush. Read it here, inside the session, or serializing the row
            # later triggers lazy IO outside the greenlet context and the request 500s.
            await session.refresh(row, ["updated_at"])
            aircraft = await session.get(Aircraft, row.aircraft_id)
            data = lease_json(row, aircraft=aircraft)
        return success_response(request=request, response=response, data=data)
    except IntegrityError as _ex:
        code, msg = integrity_error(_ex)
        return warning_response(request=request, response=response, msg=msg, status_code=code)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.delete(path="/leases/{lease_id}",
               description="Remove a lease record entered in error. A lease that simply ended is "
                           "NOT deleted — it keeps its period and the next row supersedes it.",
               responses=build_responses(include=_OK))
async def delete_lease(request: Request, response: Response, lease_id: int,
                       token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        async with request.app.state.db_client.session(DB) as session:
            await set_actor(session, token)
            row = (await session.execute(
                select(AircraftLease).where(AircraftLease.id == lease_id).options(*_LEASE_LOAD)
            )).scalar_one_or_none()
            if row is None:
                return warning_response(request=request, response=response,
                                        msg=f"Lease {lease_id} not found",
                                        status_code=status.HTTP_404_NOT_FOUND)
            aircraft = await session.get(Aircraft, row.aircraft_id)
            data = lease_json(row, aircraft=aircraft)
            await session.delete(row)
        return success_response(request=request, response=response, data=data,
                                msg="Lease record deleted")
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)
