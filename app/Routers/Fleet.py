"""The insured airframes: `/fleet/aircraft-types`, `/fleet/engine-types`, `/fleet/aircraft`
and their engines.

`fleet.aircraft` is the durable record of an airframe this business has insured. It is NOT the
FlightRadar tracking fleet — that is `cirium.asg_*`, derived weekly from Cirium and rebuilt
wholesale — and the two are deliberately unconnected: one says "we have a contract on this
airframe", the other says "poll this tail for positions".

Identity is the MSN. `POST /fleet/aircraft` finds an existing airframe by MSN first and only then
by registration, so an aircraft entered before a re-registration is recognised afterwards rather
than duplicated. `registration` and `airline` are CURRENT state and are overwritten when they
change; nothing is lost, because every change lands in `audit.change_log` with its date and actor.

ENGINES ARE INSTALLATIONS, not slots. Recording a swap is a POST of a new row at the same position
with a later date — never a PATCH of the old one — so the position keeps its history and the fitted
engine is simply the newest. Every engine the API returns carries `fitted: true|false`.

THE TWO TYPE REFERENCES ARE THE SAME SHAPE. An airframe type and an engine model are both a
(manufacturer, master series) pair loaded from Cirium, so `/fleet/aircraft-types` and
`/fleet/engine-types` take the same parameters and answer the same payload — one portal component
drives both. Naming a series that several manufacturers build, without saying which, is a 400 that
lists them rather than a guess.
"""
from datetime import date
from typing import Optional

from fastapi import Request, Response, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from Config import setup_logger
from settings import Router
from Database import ApiToken
from Database.FleetModels import (
    Aircraft, AircraftType, AircraftEngine, EngineType, MAX_ENGINES,
)
from Database.LeasingModels import AircraftLease
from Database.PolicyModels import Coverage
from api_auth import authorize, SCOPE_INSURANCE_READ, SCOPE_INSURANCE_WRITE
from Utils import success_response, warning_response, error_response
from Utils.ResponsesFunc import build_responses
from Utils.DomainCommon import (
    DB, norm_reg, set_actor, apply_sort, SortError, AmbiguousType, integrity_error, find_aircraft,
    get_or_create_airline, get_or_create_aircraft_type, get_or_create_engine_type,
    aircraft_json, type_json, engine_json, fitted_engine_ids,
    lease_json, coverage_json, lease_in_force, covers,
)

logger = setup_logger("fleet_api")

router = Router(prefix="/fleet", tags=["Fleet"])

_TYPE_SORTS = {
    "manufacturer": AircraftType.manufacturer,
    "master_series": AircraftType.master_series,
    "created_at": AircraftType.created_at,
}
_AIRCRAFT_SORTS = {
    "registration": Aircraft.registration,
    "msn": Aircraft.msn,
    "created_at": Aircraft.created_at,
    "updated_at": Aircraft.updated_at,
}

_READ = [Depends(authorize(SCOPE_INSURANCE_READ))]
_OK = {status.HTTP_200_OK, status.HTTP_400_BAD_REQUEST, status.HTTP_404_NOT_FOUND,
       status.HTTP_409_CONFLICT, status.HTTP_500_INTERNAL_SERVER_ERROR}

_AIRCRAFT_LOAD = (
    selectinload(Aircraft.aircraft_type),
    selectinload(Aircraft.airline),
    selectinload(Aircraft.engines).selectinload(AircraftEngine.engine_type),
)


# ==============================================================================================
# bodies
# ==============================================================================================

class TypeIn(BaseModel):
    """A type is the manufacturer AND the master series together — 48 series in Cirium are built
    by more than one manufacturer, so the series alone is not the identity."""
    master_series: str = Field(min_length=1, max_length=128, description="e.g. 'A320-232'.")
    manufacturer: Optional[str] = Field(default=None, max_length=128, description="e.g. 'Airbus'.")
    template_url: Optional[str] = Field(
        default=None, max_length=2048,
        description="URL of the outline drawing in the platform image store — a link, not bytes.")


class TypePatch(BaseModel):
    master_series: Optional[str] = Field(default=None, min_length=1, max_length=128)
    manufacturer: Optional[str] = Field(default=None, max_length=128)
    template_url: Optional[str] = Field(default=None, max_length=2048)


class EngineTypeIn(BaseModel):
    """An engine model. Cirium's Engine Master Series level — 'V2500-A5', 'CFM56-5' — which is the
    same granularity `TypeIn` holds for airframes."""
    master_series: str = Field(min_length=1, max_length=128, description="e.g. 'CFM56-5'.")
    manufacturer: Optional[str] = Field(default=None, max_length=128,
                                        description="e.g. 'CFM International'.")


class EngineTypePatch(BaseModel):
    master_series: Optional[str] = Field(default=None, min_length=1, max_length=128)
    manufacturer: Optional[str] = Field(default=None, max_length=128)


class EngineIn(BaseModel):
    """One installation. The MODEL is named, not spelled out: `engine_type` is the master series
    and is found-or-created in fleet.engine_type, with `engine_manufacturer` to disambiguate a
    series several builders make. What stays on this row is what is true of THIS engine — its
    serial, when it went on, the note.

    Positions are the aircraft's own left-to-right as the PILOT sees it: two engines 1=left
    2=right; three 1=left 2=centre/tail 3=right; four 1=left outboard, 2=left inboard,
    3=right inboard, 4=right outboard."""
    position: int = Field(ge=1, le=MAX_ENGINES)
    engine_type: Optional[str] = Field(default=None, max_length=128, description="e.g. 'CFM56-5'.")
    engine_manufacturer: Optional[str] = Field(default=None, max_length=128)
    engine_type_id: Optional[int] = Field(default=None, description="Instead of the two names.")
    msn: Optional[str] = Field(default=None, max_length=64)
    installed_on: Optional[date] = None
    details: Optional[str] = None


class EnginePatch(BaseModel):
    position: Optional[int] = Field(default=None, ge=1, le=MAX_ENGINES)
    engine_type: Optional[str] = Field(default=None, max_length=128)
    engine_manufacturer: Optional[str] = Field(default=None, max_length=128)
    engine_type_id: Optional[int] = None
    msn: Optional[str] = Field(default=None, max_length=64)
    installed_on: Optional[date] = None
    details: Optional[str] = None


class AircraftIn(BaseModel):
    """`aircraft_type` and `airline` are NAMES, found-or-created — the caller never deals with
    surrogate ids. Engines can be sent in the same call."""
    registration: str = Field(min_length=1, max_length=32)
    msn: Optional[str] = Field(default=None, max_length=64)
    aircraft_type: Optional[str] = Field(default=None, max_length=128)
    manufacturer: Optional[str] = Field(default=None, max_length=128,
                                        description="Only used when the type has to be created.")
    airline: Optional[str] = Field(default=None, max_length=256)
    engines: list[EngineIn] = Field(default_factory=list)


class AircraftPatch(BaseModel):
    registration: Optional[str] = Field(default=None, min_length=1, max_length=32)
    msn: Optional[str] = Field(default=None, max_length=64)
    aircraft_type: Optional[str] = Field(default=None, max_length=128)
    manufacturer: Optional[str] = Field(default=None, max_length=128)
    airline: Optional[str] = Field(default=None, max_length=256)


# ==============================================================================================
# the two type references — airframes and engines, keyed and served identically
# ==============================================================================================
# Both are (manufacturer, master series) pairs and both are loaded from Cirium, so the endpoints
# are deliberately the same shape: the portal can drive them with one component.

def _type_sortmap(model):
    return {"manufacturer": model.manufacturer, "master_series": model.master_series,
            "created_at": model.created_at}


async def _list_types(request, response, model, to_json, q, manufacturer, limit, offset,
                      sort, order):
    conds = []
    q = (q or "").strip()
    if q:
        conds.append(or_(model.master_series.ilike(f"%{q}%"), model.manufacturer.ilike(f"%{q}%")))
    if manufacturer:
        conds.append(model.manufacturer_normalized == manufacturer.strip().upper())
    stmt = apply_sort(select(model).where(*conds), sort=sort, order=order,
                      sortmap=_type_sortmap(model), tiebreak=(model.master_series, model.id))
    async with request.app.state.db_client.read_session(DB) as session:
        total = (await session.execute(
            select(func.count()).select_from(model).where(*conds))).scalar_one()
        rows = (await session.execute(stmt.limit(limit).offset(offset))).scalars().all()
    return success_response(request=request, response=response,
                            data={"items": [to_json(t) for t in rows], "total": total})


async def _create_type(request, response, model, to_json, body, token, extra: dict):
    async with request.app.state.db_client.session(DB) as session:
        await set_actor(session, token)
        row = model(master_series=body.master_series.strip(), manufacturer=body.manufacturer,
                    **extra)
        session.add(row)
        await session.flush()
        data = to_json(row)
    return success_response(request=request, response=response, data=data,
                            status_code=status.HTTP_201_CREATED)


async def _update_type(request, response, model, to_json, type_id, fields, token, subject):
    async with request.app.state.db_client.session(DB) as session:
        await set_actor(session, token)
        row = await session.get(model, type_id)
        if row is None:
            return warning_response(request=request, response=response,
                                    msg=f"{subject} {type_id} not found",
                                    status_code=status.HTTP_404_NOT_FOUND)
        for key, value in fields.items():
            setattr(row, key, value.strip() if key == "master_series" and value else value)
        await session.flush()
        data = to_json(row)
    return success_response(request=request, response=response, data=data)


async def _delete_type(request, response, model, to_json, type_id, token, subject,
                       user_model, user_column):
    async with request.app.state.db_client.session(DB) as session:
        await set_actor(session, token)
        row = await session.get(model, type_id)
        if row is None:
            return warning_response(request=request, response=response,
                                    msg=f"{subject} {type_id} not found",
                                    status_code=status.HTTP_404_NOT_FOUND)
        used = (await session.execute(
            select(func.count()).select_from(user_model)
            .where(user_column == type_id))).scalar_one()
        if used:
            return warning_response(
                request=request, response=response,
                msg=f"{used} record(s) still use this {subject.lower()} — reassign them first.",
                status_code=status.HTTP_409_CONFLICT)
        data = to_json(row)
        await session.delete(row)
    return success_response(request=request, response=response, data=data,
                            msg=f"{subject} deleted")


_TYPE_LIST_DESC = (
    "one row per manufacturer AND master series. `q` matches either; `manufacturer` pins a series "
    "several builders make. Returns `{items, total}`."
)


# --- aircraft types ---------------------------------------------------------------------------

@router.get(path="/aircraft-types", description="Airframe types — " + _TYPE_LIST_DESC,
            responses=build_responses(include=_OK), dependencies=_READ)
async def list_aircraft_types(
    request: Request, response: Response, q: str = Query(""),
    manufacturer: Optional[str] = Query(None, description="Exact manufacturer name."),
    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
    sort: Optional[str] = Query(None), order: Optional[str] = Query(None),
):
    try:
        return await _list_types(request, response, AircraftType, type_json, q, manufacturer,
                                 limit, offset, sort, order)
    except SortError as _ex:
        return warning_response(request=request, response=response, msg=str(_ex))
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.post(path="/aircraft-types",
             description="Add an airframe type. The manufacturer and master series are unique "
                         "TOGETHER — 48 series in Cirium are built by more than one manufacturer.",
             responses=build_responses(include=_OK | {status.HTTP_201_CREATED}))
async def create_aircraft_type(request: Request, response: Response, body: TypeIn,
                               token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        return await _create_type(request, response, AircraftType, type_json, body, token,
                                  {"template_url": body.template_url})
    except IntegrityError as _ex:
        code, msg = integrity_error(_ex)
        return warning_response(request=request, response=response, msg=msg, status_code=code)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.patch(path="/aircraft-types/{type_id}", description="Change an airframe type.",
              responses=build_responses(include=_OK))
async def update_aircraft_type(request: Request, response: Response, type_id: int, body: TypePatch,
                               token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        return await _update_type(request, response, AircraftType, type_json, type_id,
                                  body.model_dump(exclude_unset=True), token, "Aircraft type")
    except IntegrityError as _ex:
        code, msg = integrity_error(_ex)
        return warning_response(request=request, response=response, msg=msg, status_code=code)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.delete(path="/aircraft-types/{type_id}",
               description="Remove an airframe type. Refused with 409 while an aircraft uses it.",
               responses=build_responses(include=_OK))
async def delete_aircraft_type(request: Request, response: Response, type_id: int,
                               token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        return await _delete_type(request, response, AircraftType, type_json, type_id, token,
                                  "Aircraft type", Aircraft, Aircraft.aircraft_type_id)
    except IntegrityError as _ex:
        code, msg = integrity_error(_ex)
        return warning_response(request=request, response=response, msg=msg, status_code=code)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


# --- engine types -----------------------------------------------------------------------------

@router.get(path="/engine-types", description="Engine models — " + _TYPE_LIST_DESC +
            " Holds Cirium's Engine Master Series level (V2500-A5, CFM56-5), the same granularity "
            "the airframe side uses.",
            responses=build_responses(include=_OK), dependencies=_READ)
async def list_engine_types(
    request: Request, response: Response, q: str = Query(""),
    manufacturer: Optional[str] = Query(None, description="Exact manufacturer name."),
    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
    sort: Optional[str] = Query(None), order: Optional[str] = Query(None),
):
    try:
        return await _list_types(request, response, EngineType, type_json, q, manufacturer,
                                 limit, offset, sort, order)
    except SortError as _ex:
        return warning_response(request=request, response=response, msg=str(_ex))
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.post(path="/engine-types",
             description="Add an engine model. Manufacturer and master series are unique together, "
                         "exactly as for airframe types.",
             responses=build_responses(include=_OK | {status.HTTP_201_CREATED}))
async def create_engine_type(request: Request, response: Response, body: EngineTypeIn,
                             token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        return await _create_type(request, response, EngineType, type_json, body, token, {})
    except IntegrityError as _ex:
        code, msg = integrity_error(_ex)
        return warning_response(request=request, response=response, msg=msg, status_code=code)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.patch(path="/engine-types/{type_id}", description="Change an engine model.",
              responses=build_responses(include=_OK))
async def update_engine_type(request: Request, response: Response, type_id: int,
                             body: EngineTypePatch,
                             token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        return await _update_type(request, response, EngineType, type_json, type_id,
                                  body.model_dump(exclude_unset=True), token, "Engine type")
    except IntegrityError as _ex:
        code, msg = integrity_error(_ex)
        return warning_response(request=request, response=response, msg=msg, status_code=code)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.delete(path="/engine-types/{type_id}",
               description="Remove an engine model. Refused with 409 while an installation uses it.",
               responses=build_responses(include=_OK))
async def delete_engine_type(request: Request, response: Response, type_id: int,
                             token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        return await _delete_type(request, response, EngineType, type_json, type_id, token,
                                  "Engine type", AircraftEngine, AircraftEngine.engine_type_id)
    except IntegrityError as _ex:
        code, msg = integrity_error(_ex)
        return warning_response(request=request, response=response, msg=msg, status_code=code)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


# ==============================================================================================
# aircraft
# ==============================================================================================

@router.get(
    path="/aircraft",
    description=(
        "The insured airframes. `q` matches the registration (separator-insensitive, so 'YLLTD' "
        "finds 'YL-LTD') or the MSN; `airline_id` and `type_id` narrow further. Each row carries "
        "its type, airline and engines, so a grid with technical columns needs no follow-up call. "
        "Returns `{items, total}`."
    ),
    responses=build_responses(include=_OK), dependencies=_READ,
)
async def list_aircraft(
    request: Request, response: Response,
    q: str = Query("", description="Registration (separator-insensitive) or MSN."),
    airline_id: Optional[int] = Query(None), type_id: Optional[int] = Query(None),
    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
    sort: Optional[str] = Query(None), order: Optional[str] = Query(None),
):
    try:
        conds = []
        q = q.strip()
        if q:
            conds.append(or_(Aircraft.registration_normalized.like(f"%{norm_reg(q)}%"),
                             Aircraft.msn.ilike(f"%{q}%")))
        if airline_id is not None:
            conds.append(Aircraft.airline_id == airline_id)
        if type_id is not None:
            conds.append(Aircraft.aircraft_type_id == type_id)

        stmt = apply_sort(select(Aircraft).where(*conds).options(*_AIRCRAFT_LOAD),
                          sort=sort, order=order, sortmap=_AIRCRAFT_SORTS,
                          tiebreak=(Aircraft.registration, Aircraft.id))
        async with request.app.state.db_client.read_session(DB) as session:
            total = (await session.execute(
                select(func.count()).select_from(Aircraft).where(*conds))).scalar_one()
            rows = (await session.execute(stmt.limit(limit).offset(offset))).scalars().all()
            data = {"items": [aircraft_json(a) for a in rows], "total": total}
        return success_response(request=request, response=response, data=data)
    except SortError as _ex:
        return warning_response(request=request, response=response, msg=str(_ex))
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.post(
    path="/aircraft",
    description=(
        "Add an airframe, or fill in one already known. The aircraft is looked up by MSN first and "
        "only then by registration, so re-entering a re-registered aircraft UPDATES it instead of "
        "creating a twin — the response says which happened. `aircraft_type` and `airline` are "
        "names and are found-or-created."
    ),
    responses=build_responses(include=_OK | {status.HTTP_201_CREATED}),
)
async def create_aircraft(request: Request, response: Response, body: AircraftIn,
                          token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        async with request.app.state.db_client.session(DB) as session:
            await set_actor(session, token)
            ac_type = await get_or_create_aircraft_type(session, body.aircraft_type, body.manufacturer)
            airline = await get_or_create_airline(session, body.airline)

            row = await find_aircraft(session, registration=body.registration, msn=body.msn)
            created = row is None
            if created:
                row = Aircraft(registration=body.registration.strip(),
                               msn=body.msn.strip() if body.msn else None)
                session.add(row)
            else:
                # known airframe: follow a re-registration and fill gaps, never blank a value out
                if body.msn and not row.msn:
                    row.msn = body.msn.strip()
                if norm_reg(body.registration) != row.registration_normalized:
                    row.registration = body.registration.strip()
            if ac_type is not None:
                row.aircraft_type_id = ac_type.id
            if airline is not None:
                row.airline_id = airline.id
            await session.flush()

            for engine in body.engines:
                session.add(AircraftEngine(aircraft_id=row.id,
                                           **await _engine_fields(session, engine)))
            await session.flush()
            await session.refresh(row, ["aircraft_type", "airline", "engines"])
            data = aircraft_json(row)
        return success_response(
            request=request, response=response, data=data,
            msg="Aircraft created" if created else "Aircraft already known — updated in place",
            status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK)
    except AmbiguousType as _ex:
        return warning_response(request=request, response=response, msg=str(_ex))
    except IntegrityError as _ex:
        code, msg = integrity_error(_ex)
        return warning_response(request=request, response=response, msg=msg, status_code=code)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


async def _aircraft_card(session, row: Aircraft, on: date, history: bool) -> dict:
    """Everything known about one airframe: identity, engines, the lease terms and the policy in
    force on `on`, and — unless `history=false` — every lease and every coverage it has ever had."""
    leases = (await session.execute(
        select(AircraftLease).where(AircraftLease.aircraft_id == row.id)
        .options(selectinload(AircraftLease.agreement))
        .order_by(AircraftLease.effective_date.desc(), AircraftLease.id.desc())
    )).scalars().all()
    coverages = (await session.execute(
        select(Coverage).where(Coverage.aircraft_id == row.id)
        .options(selectinload(Coverage.policy))
        .order_by(Coverage.covered_from.desc(), Coverage.id.desc())
    )).scalars().all()

    current_lease = lease_in_force(leases, on)
    current_cover = next((c for c in coverages if covers(c, on)), None)
    out = aircraft_json(row)
    out["as_of"] = on.isoformat()
    out["lease"] = lease_json(current_lease, on=on)
    out["coverage"] = coverage_json(current_cover)
    if history:
        out["lease_history"] = [lease_json(l, on=on) for l in leases]
        out["coverage_history"] = [coverage_json(c) for c in coverages]
    return out


@router.get(
    path="/aircraft/by-registration/{registration}",
    description=(
        "The aircraft card, looked up by tail number — separator-insensitive, so 'YLLTD' finds "
        "'YL-LTD'. Pass `msn` to disambiguate when two airframes have shared a registration. "
        "Same payload as GET /fleet/aircraft/{aircraft_id}."
    ),
    responses=build_responses(include=_OK), dependencies=_READ,
)
async def get_aircraft_by_registration(
    request: Request, response: Response, registration: str,
    msn: Optional[str] = Query(None, description="Disambiguates a reused tail number."),
    on_date: Optional[date] = Query(None, description="Which day to read the lease and policy for. Default today."),
    history: bool = Query(True, description="Include every lease and coverage ever held."),
):
    try:
        on = on_date or date.today()
        async with request.app.state.db_client.read_session(DB) as session:
            row = await find_aircraft(session, registration=registration, msn=msn)
            if row is None:
                return warning_response(request=request, response=response,
                                        msg=f"No aircraft matches '{registration}'",
                                        status_code=status.HTTP_404_NOT_FOUND)
            await session.refresh(row, ["aircraft_type", "airline", "engines"])
            data = await _aircraft_card(session, row, on, history)
        return success_response(request=request, response=response, data=data)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.get(
    path="/aircraft/{aircraft_id}",
    description=(
        "Everything known about one airframe: its identity and engines, the lease terms and the "
        "policy in force on `on_date` (today by default), and its full lease and coverage history."
    ),
    responses=build_responses(include=_OK), dependencies=_READ,
)
async def get_aircraft(
    request: Request, response: Response, aircraft_id: int,
    on_date: Optional[date] = Query(None), history: bool = Query(True),
):
    try:
        on = on_date or date.today()
        async with request.app.state.db_client.read_session(DB) as session:
            row = (await session.execute(
                select(Aircraft).where(Aircraft.id == aircraft_id).options(*_AIRCRAFT_LOAD)
            )).scalar_one_or_none()
            if row is None:
                return warning_response(request=request, response=response,
                                        msg=f"Aircraft {aircraft_id} not found",
                                        status_code=status.HTTP_404_NOT_FOUND)
            data = await _aircraft_card(session, row, on, history)
        return success_response(request=request, response=response, data=data)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.patch(
    path="/aircraft/{aircraft_id}",
    description=(
        "Correct an airframe, or record a re-registration or a change of operator. Only the fields "
        "sent are touched and the previous values stay in the audit log — a re-registration is a "
        "PATCH here, not a second aircraft."
    ),
    responses=build_responses(include=_OK),
)
async def update_aircraft(request: Request, response: Response, aircraft_id: int,
                          body: AircraftPatch,
                          token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        fields = body.model_dump(exclude_unset=True)
        async with request.app.state.db_client.session(DB) as session:
            await set_actor(session, token)
            row = (await session.execute(
                select(Aircraft).where(Aircraft.id == aircraft_id).options(*_AIRCRAFT_LOAD)
            )).scalar_one_or_none()
            if row is None:
                return warning_response(request=request, response=response,
                                        msg=f"Aircraft {aircraft_id} not found",
                                        status_code=status.HTTP_404_NOT_FOUND)
            if "aircraft_type" in fields:
                ac_type = await get_or_create_aircraft_type(
                    session, fields.pop("aircraft_type"), fields.pop("manufacturer", None))
                row.aircraft_type_id = ac_type.id if ac_type else None
            fields.pop("manufacturer", None)
            if "airline" in fields:
                airline = await get_or_create_airline(session, fields.pop("airline"))
                row.airline_id = airline.id if airline else None
            for key, value in fields.items():
                setattr(row, key, value.strip() if isinstance(value, str) else value)
            await session.flush()
            await session.refresh(row, ["aircraft_type", "airline", "engines"])
            data = aircraft_json(row)
        return success_response(request=request, response=response, data=data)
    except AmbiguousType as _ex:
        return warning_response(request=request, response=response, msg=str(_ex))
    except IntegrityError as _ex:
        code, msg = integrity_error(_ex)
        return warning_response(request=request, response=response, msg=msg, status_code=code)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.delete(
    path="/aircraft/{aircraft_id}",
    description=(
        "Remove an airframe entered in error. Its engines go with it; its lease terms and coverage "
        "do NOT — those are refused with 409 while they exist, because deleting an aircraft must "
        "never silently take a contract with it. The full pre-image stays in the audit log."
    ),
    responses=build_responses(include=_OK),
)
async def delete_aircraft(request: Request, response: Response, aircraft_id: int,
                          token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        async with request.app.state.db_client.session(DB) as session:
            await set_actor(session, token)
            row = (await session.execute(
                select(Aircraft).where(Aircraft.id == aircraft_id).options(*_AIRCRAFT_LOAD)
            )).scalar_one_or_none()
            if row is None:
                return warning_response(request=request, response=response,
                                        msg=f"Aircraft {aircraft_id} not found",
                                        status_code=status.HTTP_404_NOT_FOUND)
            leases = (await session.execute(
                select(func.count()).select_from(AircraftLease)
                .where(AircraftLease.aircraft_id == aircraft_id))).scalar_one()
            covers_n = (await session.execute(
                select(func.count()).select_from(Coverage)
                .where(Coverage.aircraft_id == aircraft_id))).scalar_one()
            if leases or covers_n:
                return warning_response(
                    request=request, response=response,
                    msg=(f"This aircraft still has {leases} lease record(s) and {covers_n} "
                         f"coverage record(s) — remove those first."),
                    status_code=status.HTTP_409_CONFLICT)
            data = aircraft_json(row)
            await session.delete(row)
        return success_response(request=request, response=response, data=data,
                                msg="Aircraft deleted")
    except IntegrityError as _ex:
        code, msg = integrity_error(_ex)
        return warning_response(request=request, response=response, msg=msg, status_code=code)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


# ==============================================================================================
# engines
# ==============================================================================================

async def _engine_fields(session, body, *, partial: bool = False) -> dict:
    """Turn an engine body into column values, resolving the model name to `engine_type_id`.

    `partial` is for PATCH: only the fields actually sent are returned, so an unmentioned column is
    left alone. Sending `engine_type_id` explicitly wins over the names; sending `engine_type: null`
    clears the link.
    """
    fields = body.model_dump(exclude_unset=True) if partial else body.model_dump()
    series = fields.pop("engine_type", None)
    manufacturer = fields.pop("engine_manufacturer", None)
    explicit = fields.pop("engine_type_id", None)
    if explicit is not None:
        fields["engine_type_id"] = explicit
    elif series:
        engine_type = await get_or_create_engine_type(session, series, manufacturer)
        fields["engine_type_id"] = engine_type.id if engine_type else None
    elif series is None and not partial:
        fields["engine_type_id"] = None
    return fields

@router.get(path="/aircraft/{aircraft_id}/engines",
            description="Every engine ever recorded on this airframe, newest installation first "
                        "per position. `fitted` marks the one currently bolted on.",
            responses=build_responses(include=_OK), dependencies=_READ)
async def list_engines(request: Request, response: Response, aircraft_id: int,
                       fitted_only: bool = Query(False, description="Only the current set.")):
    try:
        async with request.app.state.db_client.read_session(DB) as session:
            rows = (await session.execute(
                select(AircraftEngine).where(AircraftEngine.aircraft_id == aircraft_id)
                .order_by(AircraftEngine.position,
                          AircraftEngine.installed_on.desc().nulls_last(),
                          AircraftEngine.id.desc())
            )).scalars().all()
        fitted = fitted_engine_ids(rows)
        items = [engine_json(e, fitted=e.id in fitted) for e in rows
                 if not fitted_only or e.id in fitted]
        return success_response(request=request, response=response,
                                data={"items": items, "total": len(items)})
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.post(
    path="/aircraft/{aircraft_id}/engines",
    description=(
        "Record an engine installation. A SWAP is a new row at the same position with a later "
        "`installed_on` — do not patch the old row, or the position loses its history. Two "
        "installations cannot share a position and a date."
    ),
    responses=build_responses(include=_OK | {status.HTTP_201_CREATED}),
)
async def add_engine(request: Request, response: Response, aircraft_id: int, body: EngineIn,
                     token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        async with request.app.state.db_client.session(DB) as session:
            await set_actor(session, token)
            aircraft = await session.get(Aircraft, aircraft_id)
            if aircraft is None:
                return warning_response(request=request, response=response,
                                        msg=f"Aircraft {aircraft_id} not found",
                                        status_code=status.HTTP_404_NOT_FOUND)
            row = AircraftEngine(aircraft_id=aircraft_id,
                                 **await _engine_fields(session, body))
            session.add(row)
            await session.flush()
            await session.refresh(row, ["engine_type"])
            data = engine_json(row, fitted=True)
        return success_response(request=request, response=response, data=data,
                                status_code=status.HTTP_201_CREATED)
    except AmbiguousType as _ex:
        return warning_response(request=request, response=response, msg=str(_ex))
    except IntegrityError as _ex:
        code, msg = integrity_error(_ex)
        return warning_response(request=request, response=response, msg=msg, status_code=code)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.patch(path="/engines/{engine_id}",
              description="Correct an engine record. To record a SWAP, POST a new installation "
                          "instead — patching rewrites history rather than adding to it.",
              responses=build_responses(include=_OK))
async def update_engine(request: Request, response: Response, engine_id: int, body: EnginePatch,
                        token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        fields = body.model_dump(exclude_unset=True)
        async with request.app.state.db_client.session(DB) as session:
            await set_actor(session, token)
            row = await session.get(AircraftEngine, engine_id)
            if row is None:
                return warning_response(request=request, response=response,
                                        msg=f"Engine {engine_id} not found",
                                        status_code=status.HTTP_404_NOT_FOUND)
            for key, value in (await _engine_fields(session, body, partial=True)).items():
                setattr(row, key, value)
            await session.flush()
            await session.refresh(row, ["engine_type"])
            data = engine_json(row)
        return success_response(request=request, response=response, data=data)
    except AmbiguousType as _ex:
        return warning_response(request=request, response=response, msg=str(_ex))
    except IntegrityError as _ex:
        code, msg = integrity_error(_ex)
        return warning_response(request=request, response=response, msg=msg, status_code=code)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.delete(path="/engines/{engine_id}",
               description="Remove an engine record entered in error. The pre-image stays in the "
                           "audit log.",
               responses=build_responses(include=_OK))
async def delete_engine(request: Request, response: Response, engine_id: int,
                        token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        async with request.app.state.db_client.session(DB) as session:
            await set_actor(session, token)
            row = await session.get(AircraftEngine, engine_id)
            if row is None:
                return warning_response(request=request, response=response,
                                        msg=f"Engine {engine_id} not found",
                                        status_code=status.HTTP_404_NOT_FOUND)
            data = engine_json(row)
            await session.delete(row)
        return success_response(request=request, response=response, data=data, msg="Engine deleted")
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)
