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

THE SERVICE BLOCK is one row per aircraft in `fleet.service_info` — source, insurance status, the
airframe's usage status as Cirium words it, whether the agreed value depreciates, and the two
contract currencies. It is created with the aircraft (default source `cirium`) and changed at
`/fleet/aircraft/{id}/service`. Bookkeeping metadata, not maintenance. The two STATUSES are shown
but never accepted: `status` follows the coverage in force today (a trigger on policy.coverage),
`usage_status` the newest Cirium revision (refreshed for a new aircraft here, and for the whole
fleet by external-worker after each revision).

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
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select, func, or_, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload, selectinload

from Config import setup_logger
from settings import Router
from Database import ApiToken
from Database.RefModels import Airline
from Database.FleetModels import (
    Aircraft, AircraftType, AircraftEngine, EngineType, ServiceInfo,
    RecordSource, AircraftCategory, MAX_ENGINES,
)
from Database.LeasingModels import AircraftLease, Agreement
from Database.PolicyModels import Coverage
from api_auth import authorize, SCOPE_INSURANCE_READ, SCOPE_INSURANCE_WRITE
from Utils import success_response, warning_response, error_response
from Utils.ResponsesFunc import build_responses
from Utils.DomainCache import AIRCRAFT_TYPE, AIRLINE, ENGINE_TYPE, FLEET, cached, invalidate
from Utils.DomainCommon import (
    normalize_template_urls, merge_template_urls, reload_with, page_with_total,
    AIRCRAFT_GRID, AIRCRAFT_ONE, AIRCRAFT_BRIEF, ENGINE_LOAD,
    DB, norm, norm_reg, set_actor, apply_sort, SortError, AmbiguousType, integrity_error, find_aircraft,
    get_or_create_airline, get_or_create_aircraft_type, get_or_create_engine_type,
    aircraft_json, type_json, engine_json, fitted_engine_ids, service_json,
    lease_json, coverage_json, lease_in_force, covers, COVERAGE_LOAD,
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

# The loader sets live in Utils/DomainCommon beside the serializers that decide what they
# must contain — Leasing and Policies render aircraft too, and three copies of the rule
# would be three chances to get it wrong. Local names kept so the call sites read the same.
_AIRCRAFT_LOAD = AIRCRAFT_GRID
_AIRCRAFT_ONE = AIRCRAFT_ONE


# ==============================================================================================
# bodies
# ==============================================================================================

class TemplateUrls(BaseModel):
    """The outline drawings a type can have, one per state the aircraft is damaged in: gear up in
    the air, gear down and doors open on the stand. URLs into the platform image store — links,
    never bytes, so a grid of a hundred types stays a grid of a hundred rows."""
    airborne: Optional[str] = Field(default=None, max_length=2048)
    on_the_ground: Optional[str] = Field(default=None, max_length=2048)


class TypeIn(BaseModel):
    """An airframe type is the manufacturer, the master series AND the category together. 48
    series in Cirium are built by more than one manufacturer, and a series flown in two roles is
    two rows: an A300-600 freighter and an A300-600 in passenger layout are not the same thing to
    insure. All three make the identity; none of them alone does."""
    master_series: str = Field(min_length=1, max_length=128, description="e.g. 'A320-232'.")
    manufacturer: Optional[str] = Field(default=None, max_length=128, description="e.g. 'Airbus'.")
    category: AircraftCategory = Field(
        default=AircraftCategory.PASSENGER,
        description="passenger (airline AND business aviation), cargo (freight and the "
                    "convertibles) or other (military, training, EMS, utility — neither of the "
                    "first two). Defaults to passenger.")
    template_url: Optional[TemplateUrls] = Field(
        default=None,
        description="The two outline drawings, `{airborne, on_the_ground}`. Omit it, or omit a "
                    "view, to record nothing for it.")


class TypePatch(BaseModel):
    master_series: Optional[str] = Field(default=None, min_length=1, max_length=128)
    manufacturer: Optional[str] = Field(default=None, max_length=128)
    category: Optional[AircraftCategory] = Field(
        default=None,
        description="Moving a type between categories re-files every aircraft on it. To split a "
                    "type instead, create the second row and re-point the aircraft that belong "
                    "to it.")
    template_url: Optional[TemplateUrls] = Field(
        default=None,
        description="Merged per VIEW, not replaced: sending only `on_the_ground` leaves the "
                    "airborne drawing alone. Send a view as null to clear it.")


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


_NOT_HERE = {
    "status": "`status` follows the policy coverage in force today",
    "usage_status": "`usage_status` comes from Cirium",
    "policy_currency": "the policy currency is set on the policy (`currency` on /policy/policies)",
}


class _NoDerivedStatus(BaseModel):
    """The service block SHOWS `status` and `usage_status`, but nobody sets them: the first is
    whether a coverage covers today, the second is Cirium's. `policy_currency` is no longer here
    at all — it is the policy's. Sending any of them is refused rather than silently dropped, so a
    client still offering them as inputs finds out at once."""

    @model_validator(mode="before")
    @classmethod
    def _refuse_derived(cls, value):
        if isinstance(value, dict):
            sent = sorted(_NOT_HERE.keys() & value.keys())
            if sent:
                raise ValueError(f"{', '.join(sent)} cannot be set here: "
                                 + "; ".join(_NOT_HERE[k] for k in sent) + ".")
        return value


class ServiceIn(_NoDerivedStatus):
    """The service block — bookkeeping metadata about the aircraft's record, not maintenance.
    Every field has a default, so sending `{}` (or nothing at all) records the ordinary case."""
    agreed_value_fixed: bool = Field(
        default=False, description="True freezes the agreed value at the preliminary figure.")
    source: RecordSource = Field(
        default=RecordSource.CIRIUM,
        description="Where the record came from. Left out: `cirium` when Cirium knows the tail, "
                    "`manual` when it does not.")
    lease_currency: str = Field(default="USD", min_length=3, max_length=3)


class ServicePatch(_NoDerivedStatus):
    agreed_value_fixed: Optional[bool] = None
    source: Optional[RecordSource] = None
    lease_currency: Optional[str] = Field(default=None, min_length=3, max_length=3)


class AircraftIn(BaseModel):
    """`aircraft_type` and `airline` are NAMES, found-or-created — the caller never deals with
    surrogate ids. Engines can be sent in the same call."""
    registration: str = Field(min_length=1, max_length=32)
    msn: Optional[str] = Field(default=None, max_length=64)
    aircraft_type: Optional[str] = Field(default=None, max_length=128)
    manufacturer: Optional[str] = Field(default=None, max_length=128,
                                        description="Disambiguates a series several builders make.")
    aircraft_category: Optional[AircraftCategory] = Field(
        default=None,
        description="Which role of that series — a freighter and a passenger aircraft of the same "
                    "series are separate types. Omit it when only one exists; a 400 lists the "
                    "choices when several do. A type that has to be CREATED defaults to passenger.")
    airline: Optional[str] = Field(default=None, max_length=256)
    mtow_kg: Optional[int] = Field(
        default=None, gt=0, le=1_000_000,
        description="Maximum take-off weight of this airframe, kg. The lookup pre-fills it from "
                    "Cirium (Operating MTOW, else Certified, converted from lbs).")
    is_asg: bool = Field(
        default=False,
        description="Only for an airline this call CREATES: TRUE files it as an ASG airline "
                    "(cirium.asg_*), FALSE — the default — as insured but not ASG "
                    "(cirium.non_asg_insured_*). An airline that already exists keeps its own flag; "
                    "change that at PATCH /ref/airlines/{id}.")
    engines: list[EngineIn] = Field(default_factory=list)
    service: ServiceIn = Field(default_factory=ServiceIn,
                               description="The service block. Omit it for the defaults.")


class BulkAircraftIn(BaseModel):
    """Several airframes at once, each in exactly the shape `POST /fleet/aircraft` takes — so an
    item of `GET /fleet/aircraft/lookup` can be sent back unchanged."""
    aircraft: list[AircraftIn] = Field(min_length=1, max_length=500)


class AircraftPatch(BaseModel):
    registration: Optional[str] = Field(default=None, min_length=1, max_length=32)
    msn: Optional[str] = Field(default=None, max_length=64)
    aircraft_type: Optional[str] = Field(default=None, max_length=128)
    manufacturer: Optional[str] = Field(default=None, max_length=128)
    aircraft_category: Optional[AircraftCategory] = Field(
        default=None, description="Which role of the series — see AircraftIn. Sending it alone "
                                  "moves the aircraft to that role of its current series, which "
                                  "is how a converted freighter is recorded.")
    airline: Optional[str] = Field(default=None, max_length=256)
    mtow_kg: Optional[int] = Field(
        default=None, gt=0, le=1_000_000,
        description="Maximum take-off weight of this airframe, kg. The lookup pre-fills it from "
                    "Cirium (Operating MTOW, else Certified, converted from lbs).")


# ==============================================================================================
# the two type references — airframes and engines, keyed and served identically
# ==============================================================================================
# Both are (manufacturer, master series) pairs and both are loaded from Cirium, so the endpoints
# are deliberately the same shape: the portal can drive them with one component.

def _type_sortmap(model):
    out = {"manufacturer": model.manufacturer, "master_series": model.master_series,
           "created_at": model.created_at}
    if hasattr(model, "category"):
        out["category"] = model.category
    return out


async def _list_types(request, response, model, to_json, entity, q, manufacturer, limit, offset,
                      sort, order, category=None):
    conds = []
    q = (q or "").strip()
    if q:
        conds.append(or_(model.master_series.ilike(f"%{q}%"), model.manufacturer.ilike(f"%{q}%")))
    if manufacturer:
        conds.append(model.manufacturer_normalized == manufacturer.strip().upper())
    if category is not None:
        conds.append(model.category == category)
    stmt = apply_sort(select(model).where(*conds), sort=sort, order=order,
                      sortmap=_type_sortmap(model), tiebreak=(model.master_series, model.id))

    # 806 airframe types and 365 engine models, read by every typeahead keystroke and written a
    # handful of times a year — the one read in this domain where a cache pays for itself.
    async def load():
        async with request.app.state.db_client.read_session(DB) as session:
            rows, total = await page_with_total(
                session, stmt, limit=limit, offset=offset,
                count_stmt=select(func.count()).select_from(model).where(*conds))
            return {"items": [to_json(t) for t in rows], "total": total}

    data = await cached(request, entity,
                        {"q": q, "manufacturer": manufacturer, "limit": limit, "offset": offset,
                         "sort": sort, "order": order,
                         # Part of the key, not decoration: leave it out and a request filtered to
                         # cargo is served the cached unfiltered page.
                         "category": getattr(category, "value", category)}, load)
    return success_response(request=request, response=response, data=data)


async def _create_type(request, response, model, to_json, entity, body, token, extra: dict):
    async with request.app.state.db_client.session(DB) as session:
        await set_actor(session, token)
        row = model(master_series=body.master_series.strip(), manufacturer=body.manufacturer,
                    **extra)
        session.add(row)
        await session.flush()
        data = to_json(row)
    await invalidate(request, entity)
    return success_response(request=request, response=response, data=data,
                            status_code=status.HTTP_201_CREATED)


async def _update_type(request, response, model, to_json, entity, type_id, fields, token, subject):
    async with request.app.state.db_client.session(DB) as session:
        await set_actor(session, token)
        row = await session.get(model, type_id)
        if row is None:
            return warning_response(request=request, response=response,
                                    msg=f"{subject} {type_id} not found",
                                    status_code=status.HTTP_404_NOT_FOUND)
        for key, value in fields.items():
            if key == "template_url":
                # The only field here that MERGES. Everything else on a type is one value that a
                # PATCH replaces; the drawings are two, and replacing the pair would mean the
                # caller had to resend a URL it may not even have in hand.
                value = merge_template_urls(row.template_url, value or {})
            elif key == "master_series" and value:
                value = value.strip()
            setattr(row, key, value)
        await session.flush()
        data = to_json(row)
    await invalidate(request, entity)
    return success_response(request=request, response=response, data=data)


async def _delete_type(request, response, model, to_json, entity, type_id, token, subject,
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
    await invalidate(request, entity)
    return success_response(request=request, response=response, data=data,
                            msg=f"{subject} deleted")


_TYPE_LIST_DESC = (
    "one row per manufacturer AND master series. `q` matches either; `manufacturer` pins a series "
    "several builders make. Returns `{items, total}`."
)
_AIRFRAME_LIST_DESC = (
    "one row per manufacturer, master series AND category, so 'Airbus A300-600' appears twice — "
    "once as the freighter, once as the passenger aircraft. `q` matches the manufacturer or the "
    "series; `manufacturer` pins a series several builders make; `category` narrows to one role. "
    "Each item carries `category` and a ready-made `label`. Returns `{items, total}`."
)


# --- aircraft types ---------------------------------------------------------------------------

@router.get(path="/aircraft-types", description="Airframe types — " + _AIRFRAME_LIST_DESC,
            responses=build_responses(include=_OK), dependencies=_READ)
async def list_aircraft_types(
    request: Request, response: Response, q: str = Query(""),
    manufacturer: Optional[str] = Query(None, description="Exact manufacturer name."),
    category: Optional[AircraftCategory] = Query(None, description="passenger | cargo | other."),
    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
    sort: Optional[str] = Query(None), order: Optional[str] = Query(None),
):
    try:
        return await _list_types(request, response, AircraftType, type_json, AIRCRAFT_TYPE,
                                 q, manufacturer, limit, offset, sort, order, category)
    except SortError as _ex:
        return warning_response(request=request, response=response, msg=str(_ex))
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.post(path="/aircraft-types",
             description="Add an airframe type. The manufacturer, master series and category are "
                         "unique TOGETHER: 48 series in Cirium are built by more than one "
                         "manufacturer, and the same series flown as a freighter and as a "
                         "passenger aircraft is two rows. `category` defaults to passenger.",
             responses=build_responses(include=_OK | {status.HTTP_201_CREATED}))
async def create_aircraft_type(request: Request, response: Response, body: TypeIn,
                               token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        return await _create_type(
            request, response, AircraftType, type_json, AIRCRAFT_TYPE, body, token,
            {"template_url": normalize_template_urls(
                body.template_url.model_dump() if body.template_url else None),
             "category": body.category})
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
        return await _update_type(request, response, AircraftType, type_json, AIRCRAFT_TYPE, type_id,
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
        return await _delete_type(request, response, AircraftType, type_json, AIRCRAFT_TYPE, type_id,
                                  token, "Aircraft type", Aircraft, Aircraft.aircraft_type_id)
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
        return await _list_types(request, response, EngineType, type_json, ENGINE_TYPE,
                                 q, manufacturer, limit, offset, sort, order)
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
        return await _create_type(request, response, EngineType, type_json, ENGINE_TYPE, body,
                                  token, {})
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
        return await _update_type(request, response, EngineType, type_json, ENGINE_TYPE, type_id,
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
        return await _delete_type(request, response, EngineType, type_json, ENGINE_TYPE, type_id,
                                  token, "Engine type", AircraftEngine, AircraftEngine.engine_type_id)
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
        # The grid the portal opens on every page load, over a fleet that changes a few times a
        # week. Cached under the FLEET generation, which the middleware bumps after ANY successful
        # write to /fleet, /ref, /leasing or /policy — so a user who saves an aircraft and is sent
        # back to the list sees their own change, and nobody had to remember to say so here.
        async def load():
            async with request.app.state.db_client.read_session(DB) as session:
                rows, total = await page_with_total(
                    session, stmt, limit=limit, offset=offset,
                    count_stmt=select(func.count()).select_from(Aircraft).where(*conds))
                return {"items": [aircraft_json(a) for a in rows], "total": total}

        data = await cached(request, FLEET,
                            {"grid": "aircraft", "q": q, "airline_id": airline_id,
                             "type_id": type_id, "limit": limit, "offset": offset,
                             "sort": sort, "order": order}, load)
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
            ac_type = await get_or_create_aircraft_type(
                session, body.aircraft_type, body.manufacturer, body.aircraft_category)
            airline = await get_or_create_airline(session, body.airline, is_asg=body.is_asg)

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
            if body.mtow_kg is not None:
                row.mtow_kg = body.mtow_kg
            if ac_type is not None:
                row.aircraft_type_id = ac_type.id
            if airline is not None:
                row.airline_id = airline.id
            await session.flush()

            # Resolve EVERY engine before adding ANY. Interleaving the two lets the SELECT
            # inside _engine_fields trigger an autoflush of the row added just before it, so the
            # engines went to the database one INSERT at a time; resolved first, they are one
            # statement however many there are.
            engine_rows = [AircraftEngine(aircraft_id=row.id, **await _engine_fields(session, e))
                           for e in body.engines]
            session.add_all(engine_rows)
            # every aircraft gets its service block; a re-post leaves an existing one alone rather
            # than resetting fields somebody has since set
            if created:
                fields = body.service.model_dump()
                fields["lease_currency"] = fields["lease_currency"].upper()
                session.add(ServiceInfo(aircraft_id=row.id, **fields))
            await session.flush()
            if created:
                await _refresh_usage_status(
                    session, [row.id],
                    unsourced=[] if "source" in body.service.model_fields_set else [row.id])
            row = await reload_with(session, Aircraft, row.id, *_AIRCRAFT_ONE)
            data = aircraft_json(row)
        # creating an aircraft can find-or-create its type, its engines' model and its
        # airline, so all three listings may have changed
        await invalidate(request, AIRCRAFT_TYPE, ENGINE_TYPE, AIRLINE)
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


# ==============================================================================================
# adding from Cirium — the lookup that fills the form, and the bulk insert behind it
# ==============================================================================================
# The lookup reads the NEWEST revision of each Cirium plan type (Commercial, Business &
# Helicopters) — the fleet as it stands — and hands back each airframe already in the shape
# `POST /fleet/aircraft` takes, category and engines included. Sending an item back unchanged is
# therefore unambiguous even for a series that exists as a freighter AND a passenger aircraft.
#
# THE AIRLINE is matched against ref.airline exactly as the tracking matviews match it: the
# Operator, Sub Lessor or Owner CONTAINS the name, longest name first ("Air Arabia Abu Dhabi" is
# Air Arabia's). An airframe no name matches carries Cirium's Operator verbatim and a null
# `airline_id` — posting it creates that airline.
#
# THE CATEGORY is Cirium's `Primary Usage` collapsed to three, the same collapse
# `_admin/load_type_categories.py` applied to the type table. Keep the two lists in step.

_PASSENGER_USAGE = (
    "Passenger", "Business - Private Company Use", "Business - Air Taxi/Air Charter",
    "Private Use", "VIP / Head of State", "Sightseeing / Tourist", "Government - Liaison",
)
_CARGO_USAGE = (
    "Freight / Cargo", "Combi / Mixed (Passenger/Cargo)",
    "Quick-Change/Convertible (Passenger/Cargo)",
)
# Statuses the tracking matviews leave out. A registration lookup still answers for them — the
# caller typed that tail — but prefers a live airframe when a tail has been reissued.
_INACTIVE = ("Cancelled", "On order", "Retired", "Written off")


def _sql_list(values) -> str:
    """Constants only — never caller input."""
    return ", ".join("'" + v.replace("'", "''") + "'" for v in values)


# Two filters are spliced in: `{match}` narrows the Cirium rows BEFORE the de-duplication (so the
# registration index-free scan touches two revisions, not the history), `{keep}` filters the
# finished rows. Every value arrives as a bind; CAST, never the double-colon form, which the
# text() bind parser misreads.
_LOOKUP_SQL = """
WITH latest AS (
    SELECT max(id) AS id FROM cirium.aircraftrevision
    WHERE plan_type IN ('Commercial', 'Business&Helicopters')
    GROUP BY plan_type
),
src AS (
    SELECT c.*,
           upper(regexp_replace(c."Registration", '[^A-Za-z0-9]', '', 'g')) AS reg_key
    FROM cirium.ciriumaircrafts c
    WHERE c.revision_id IN (SELECT id FROM latest)
      AND c."Registration" IS NOT NULL
      AND ({match})
),
one AS (
    SELECT DISTINCT ON (reg_key) *
    FROM src
    WHERE reg_key <> ''
    ORDER BY reg_key,
             ("Status" IN ({inactive})) NULLS LAST,
             revision_id DESC,
             ("Lease Type" = 'Sub Lease') DESC NULLS LAST,
             id DESC
)
SELECT o."Registration"                                   AS registration,
       nullif(btrim(o."Serial Number"), '')               AS msn,
       o."Manufacturer"                                   AS manufacturer,
       o."Master Series"                                  AS master_series,
       CASE WHEN o."Primary Usage" IN ({passenger}) THEN 'passenger'
            WHEN o."Primary Usage" IN ({cargo}) THEN 'cargo'
            ELSE 'other' END                              AS category,
       o."Operator"                                       AS operator,
       o."Status"                                         AS usage_status,
       o."Number Of Engines"                              AS engine_count,
       o."Engine Manufacturer"                            AS engine_manufacturer,
       o."Engine Master Series"                           AS engine_master_series,
       round(coalesce(o."Operating MTOW (lbs)", o."Certified MTOW (lbs)") * 0.45359237)
                                                          AS mtow_kg,
       al.id                                              AS airline_id,
       al.airline_name                                    AS airline_name,
       al.is_asg                                          AS airline_is_asg,
       fa.id                                              AS fleet_id
FROM one o
LEFT JOIN LATERAL (
    SELECT a.id, a.airline_name, a.is_asg
    FROM ref.airline a
    WHERE o."Operator"   ILIKE '%' || a.airline_name || '%'
       OR o."Sub Lessor" ILIKE '%' || a.airline_name || '%'
       OR o."Owner"      ILIKE '%' || a.airline_name || '%'
    ORDER BY length(a.airline_name) DESC, a.airline_name
    LIMIT 1
) al ON TRUE
LEFT JOIN LATERAL (
    SELECT f.id
    FROM fleet.aircraft f
    WHERE f.registration_normalized = o.reg_key
       OR f.msn = nullif(btrim(o."Serial Number"), '')
    ORDER BY (f.registration_normalized = o.reg_key) DESC, f.id
    LIMIT 1
) fa ON TRUE
WHERE {keep}
ORDER BY o."Registration", o.reg_key
"""

_COMMON = {"inactive": _sql_list(_INACTIVE), "passenger": _sql_list(_PASSENGER_USAGE),
           "cargo": _sql_list(_CARGO_USAGE)}

_BY_REGISTRATION = text(_LOOKUP_SQL.format(
    match="""upper(regexp_replace(c."Registration", '[^A-Za-z0-9]', '', 'g'))
             = CAST(:reg_key AS text)""",
    keep="TRUE", **_COMMON))

# The pre-filter is the airline's own name, which every row the LATERAL can award to it must
# contain; the LATERAL then decides, so a longer name that also matches still wins its rows.
_NAME_MATCH = """c."Operator"   ILIKE '%' || CAST(:airline_name AS text) || '%'
          OR c."Sub Lessor" ILIKE '%' || CAST(:airline_name AS text) || '%'
          OR c."Owner"      ILIKE '%' || CAST(:airline_name AS text) || '%'"""
_ACTIVE_ONLY = f"""(CAST(:include_inactive AS boolean)
                  OR o."Status" IS NULL OR o."Status" NOT IN ({_COMMON['inactive']}))"""

_BY_AIRLINE = text(_LOOKUP_SQL.format(
    match=_NAME_MATCH,
    keep=f"al.id = CAST(:airline_id AS integer) AND {_ACTIVE_ONLY}",
    **_COMMON))

# A name NOT in ref.airline competes with the names that are, under the LATERAL's own order
# (longest first, then alphabetical): a row is this name's unless a ref.airline name that also
# matches it would win. So 'Air Arabia Abu Dhabi' takes rows from 'Air Arabia', never the reverse.
_BY_AIRLINE_NAME = text(_LOOKUP_SQL.format(
    match=_NAME_MATCH,
    keep=f"""(al.id IS NULL
              OR length(al.airline_name) < length(CAST(:airline_name AS text))
              OR (length(al.airline_name) = length(CAST(:airline_name AS text))
                  AND al.airline_name > CAST(:airline_name AS text)))
             AND {_ACTIVE_ONLY}""",
    **_COMMON))

def _lookup_json(r) -> dict:
    """One Cirium airframe as a ready `POST /fleet/aircraft` body, plus what the form shows."""
    engines = []
    if r.engine_master_series:
        count = max(1, min(r.engine_count or 1, MAX_ENGINES))
        engines = [{"position": p, "engine_type": r.engine_master_series,
                    "engine_manufacturer": r.engine_manufacturer} for p in range(1, count + 1)]
    return {
        "registration": r.registration.strip(),
        "msn": r.msn,
        "aircraft_type": r.master_series,
        "manufacturer": r.manufacturer,
        "aircraft_category": r.category,
        "mtow_kg": int(r.mtow_kg) if r.mtow_kg else None,
        "airline": r.airline_name or r.operator,
        "airline_id": r.airline_id,
        # the matched airline's own flag; for one posting would create, the default it gets
        "is_asg": bool(r.airline_is_asg),
        "operator": r.operator,
        "engines": engines,
        # shown, not sent back: the service block derives it from Cirium on every read
        "usage_status": r.usage_status,
        "service": {"source": RecordSource.CIRIUM.value},
        "in_fleet": r.fleet_id is not None,
        "fleet_aircraft_id": r.fleet_id,
    }


@router.get(
    path="/aircraft/lookup",
    description=(
        "Find airframes in Cirium to add to the fleet — the newest revision of each plan type. "
        "Exactly one of the three parameters:\n\n"
        "* `registration` — one airframe, separator- and case-insensitive ('yl-abc' = 'YLABC'). "
        "Returns the object; 404 when Cirium does not know the tail. A tail reissued to several "
        "airframes answers with the one still flying.\n"
        "* `airline_id` (a `ref.airline` id) — every airframe of that airline, matched the way the "
        "tracking fleet is (Operator, Sub Lessor or Owner contains the name, longest name wins). "
        "Retired, written-off, cancelled and on-order airframes are left out unless "
        "`include_inactive=true`. Returns `{items, total}`, sorted by registration.\n"
        "* `airline` — the same, for an airline NOT yet in ref.airline, named as the Cirium "
        "catalogue writes it (`GET /airlines/`, field `airline`). The name competes with the "
        "ref.airline names under the same longest-wins rule, so it gets only the rows no "
        "ref.airline name would take. Every item carries `airline` = that name, "
        "`airline_id: null`, `is_asg: false`. A name that IS in ref.airline (case and outer "
        "spaces ignored) answers exactly as its `airline_id` would.\n\n"
        "Each item is a ready `POST /fleet/aircraft` body — `aircraft_category` and `engines` "
        "included, so sending it back is never ambiguous — plus `in_fleet` / `fleet_aircraft_id` "
        "(already in fleet.aircraft by registration or MSN), `airline_id` (null when no ref.airline "
        "name matched: `airline` is then Cirium's Operator verbatim, and posting it creates that "
        "airline) and `operator`, Cirium's own wording."
    ),
    responses=build_responses(include=_OK), dependencies=_READ,
)
async def lookup_aircraft(
    request: Request, response: Response,
    registration: Optional[str] = Query(None, max_length=32,
                                        description="Tail number, any case, any separators."),
    airline_id: Optional[int] = Query(None, description="ref.airline id."),
    airline: Optional[str] = Query(None, max_length=256,
                                   description="An airline name from the Cirium catalogue."),
    include_inactive: bool = Query(False, description="airline_id / airline only: keep retired, "
                                                      "written-off, cancelled and on-order airframes."),
    limit: int = Query(500, ge=1, le=2000), offset: int = Query(0, ge=0),
):
    try:
        reg_key = norm_reg(registration) if registration else ""
        name = airline.strip() if airline else ""
        if sum((bool(reg_key), airline_id is not None, bool(name))) != 1:
            return warning_response(
                request=request, response=response,
                msg="Send exactly one of `registration`, `airline_id` or `airline`.")

        async with request.app.state.db_client.read_session(DB) as session:
            if reg_key:
                row = (await session.execute(_BY_REGISTRATION, {"reg_key": reg_key})).first()
                if row is None:
                    return warning_response(
                        request=request, response=response,
                        msg=f"Cirium has no aircraft registered '{registration.strip()}'.",
                        status_code=status.HTTP_404_NOT_FOUND)
                return success_response(request=request, response=response,
                                        data=_lookup_json(row))

            if name:
                # already in ref.airline under this name: then it is simply that airline
                airline_id = (await session.execute(
                    select(Airline.id)
                    .where(func.upper(func.btrim(Airline.airline_name)) == norm(name))
                    .order_by(Airline.id).limit(1))).scalar_one_or_none()
            if airline_id is None:
                rows = (await session.execute(_BY_AIRLINE_NAME, {
                    "airline_name": name, "include_inactive": include_inactive})).all()
                items = [{**_lookup_json(r), "airline": name, "airline_id": None, "is_asg": False}
                         for r in rows]
            else:
                known = (await session.execute(
                    select(Airline.airline_name).where(Airline.id == airline_id)
                )).scalar_one_or_none()
                if known is None:
                    return warning_response(request=request, response=response,
                                            msg=f"Airline {airline_id} not found",
                                            status_code=status.HTTP_404_NOT_FOUND)
                rows = (await session.execute(_BY_AIRLINE, {
                    "airline_name": known, "airline_id": airline_id,
                    "include_inactive": include_inactive})).all()
                items = [_lookup_json(r) for r in rows]
        return success_response(request=request, response=response,
                                data={"items": items[offset:offset + limit], "total": len(items)})
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


class _RowErrors(Exception):
    """Rows the bulk insert cannot resolve. Raised INSIDE the session so the transaction rolls back
    whatever the rows before them had already found-or-created."""

    def __init__(self, errors: list[dict]):
        super().__init__(f"{len(errors)} row error(s)")
        self.errors = errors


class _AlreadyInFleet(Exception):
    def __init__(self, clashes: list[str]):
        super().__init__(", ".join(clashes))
        self.clashes = clashes


def _row_error(loc: tuple, msg: str) -> dict:
    # the shape RequestValidationError carries, so the ordinary 422 handler renders it —
    # `field` comes out as "body.aircraft.3.aircraft_type"
    return {"loc": ("body", "aircraft", *loc), "msg": msg, "type": "value_error"}


@router.post(
    path="/aircraft/bulk",
    description=(
        "Add up to 500 airframes in ONE transaction: all of them or none. Each item is a "
        "`POST /fleet/aircraft` body, and types, engine models and airlines are found-or-created "
        "the same way. Nothing has to be in Cirium: a tail, a type or an airline Cirium does not "
        "know is created as sent (only `registration` is required), and filed as source `manual` "
        "unless the item says otherwise.\n\n"
        "Unlike the single POST this only ADDS. An item whose registration or MSN is already in "
        "the fleet is a 409 naming every such aircraft — nothing is updated in place. Rows that "
        "cannot be resolved (a series several manufacturers build or that exists in several "
        "categories, a registration or MSN repeated within the request) are a 422 with one entry "
        "per row, `field` = `body.aircraft.<index>.<field>`.\n\n"
        "Returns 201 with the created aircraft (sorted by registration, with type, airline, "
        "service block and engines) and their `count`. Every insert is in /history, attributed "
        "to the caller, as a single create would be."
    ),
    status_code=status.HTTP_201_CREATED,
    responses=build_responses(include=_OK | {status.HTTP_201_CREATED}),
)
async def create_aircraft_bulk(request: Request, response: Response, body: BulkAircraftIn,
                               token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    # Rows that contradict each other are caught before the database is touched.
    errors, by_reg, by_msn, asg_asked = [], {}, {}, {}
    for i, item in enumerate(body.aircraft):
        key = norm_reg(item.registration)
        if not key:
            errors.append(_row_error((i, "registration"), "Registration has no letters or digits."))
        elif key in by_reg:
            errors.append(_row_error((i, "registration"),
                                     f"Same registration as row {by_reg[key]}."))
        else:
            by_reg[key] = i
        msn = item.msn.strip() if item.msn else ""
        if msn and msn in by_msn:
            errors.append(_row_error((i, "msn"), f"Same MSN as row {by_msn[msn]}."))
        elif msn:
            by_msn[msn] = i
        # Rows naming the same airline must agree on the flag it would be created with — else
        # which row wins would depend on the order they were sent in.
        if item.airline and item.airline.strip():
            first = asg_asked.setdefault(norm(item.airline), (i, item.is_asg))
            if first[1] != item.is_asg:
                errors.append(_row_error((i, "is_asg"),
                                         f"Row {first[0]} names the same airline with "
                                         f"is_asg={str(first[1]).lower()}."))
    if errors:
        raise RequestValidationError(errors)

    try:
        async with request.app.state.db_client.session(DB) as session:
            await set_actor(session, token)

            # ONE question for the whole batch: which of these are already here.
            clash = [Aircraft.registration_normalized.in_(list(by_reg))]
            if by_msn:
                clash.append(Aircraft.msn.in_(list(by_msn)))
            held = (await session.execute(
                select(Aircraft.registration, Aircraft.msn)
                .where(or_(*clash)).order_by(Aircraft.registration)
            )).all()
            if held:
                raise _AlreadyInFleet([
                    f"{h.registration}" + (f" (MSN {h.msn})" if h.msn else "") for h in held])

            # Resolve EVERYTHING before adding ANY aircraft: each find-or-create is a SELECT, and
            # a SELECT autoflushes whatever was added before it — one INSERT per aircraft instead
            # of one for the batch. Every error is collected, so the caller fixes them in one go.
            airlines: dict[str, Optional[Airline]] = {}
            resolved = []
            for i, item in enumerate(body.aircraft):
                ac_type = None
                try:
                    ac_type = await get_or_create_aircraft_type(
                        session, item.aircraft_type, item.manufacturer, item.aircraft_category)
                except AmbiguousType as ex:
                    errors.append(_row_error((i, "aircraft_type"), str(ex)))
                engines = []
                for j, engine in enumerate(item.engines):
                    try:
                        engines.append(await _engine_fields(session, engine))
                    except AmbiguousType as ex:
                        errors.append(_row_error((i, "engines", j, "engine_type"), str(ex)))
                airline_key = norm(item.airline) if item.airline and item.airline.strip() else None
                if airline_key and airline_key not in airlines:
                    # The flag only matters if the airline is CREATED here. Either way its whole
                    # Cirium fleet enters the tracking matviews (asg_* or non_asg_insured_*) at
                    # the next refresh.
                    airlines[airline_key] = await get_or_create_airline(session, item.airline,
                                                                        is_asg=item.is_asg)
                resolved.append((item, ac_type, airlines.get(airline_key), engines))
            if errors:
                raise _RowErrors(errors)

            rows = []
            for item, ac_type, airline, _ in resolved:
                rows.append(Aircraft(
                    registration=item.registration.strip(),
                    msn=item.msn.strip() if item.msn and item.msn.strip() else None,
                    aircraft_type_id=ac_type.id if ac_type else None,
                    airline_id=airline.id if airline else None,
                    mtow_kg=item.mtow_kg))
            session.add_all(rows)
            await session.flush()

            children = []
            for row, (item, _, _, engines) in zip(rows, resolved):
                fields = item.service.model_dump()
                fields["lease_currency"] = fields["lease_currency"].upper()
                children.append(ServiceInfo(aircraft_id=row.id, **fields))
                children.extend(AircraftEngine(aircraft_id=row.id, **e) for e in engines)
            session.add_all(children)
            await session.flush()
            await _refresh_usage_status(
                session, [r.id for r in rows],
                unsourced=[r.id for r, (item, *_) in zip(rows, resolved)
                           if "source" not in item.service.model_fields_set])

            created = (await session.execute(
                select(Aircraft).where(Aircraft.id.in_([r.id for r in rows]))
                .options(*_AIRCRAFT_LOAD)
                .order_by(Aircraft.registration, Aircraft.id)
                .execution_options(populate_existing=True)
            )).unique().scalars().all()
            data = {"created": [aircraft_json(a) for a in created], "count": len(created)}
        await invalidate(request, AIRCRAFT_TYPE, ENGINE_TYPE, AIRLINE)
        return success_response(request=request, response=response, data=data,
                                msg=f"{len(created)} aircraft created",
                                status_code=status.HTTP_201_CREATED)
    except _RowErrors as _ex:
        raise RequestValidationError(_ex.errors)
    except _AlreadyInFleet as _ex:
        return warning_response(
            request=request, response=response,
            msg=(f"{len(_ex.clashes)} aircraft already in the fleet — nothing was added: "
                 f"{', '.join(_ex.clashes)}"),
            status_code=status.HTTP_409_CONFLICT)
    except IntegrityError as _ex:
        code, msg = integrity_error(_ex)
        return warning_response(request=request, response=response, msg=msg, status_code=code)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


async def _refresh_usage_status(session, aircraft_ids: list[int], *,
                                unsourced: list[int]) -> None:
    """Fill a new aircraft's usage status from Cirium's newest revision — the same function the
    worker runs for the whole fleet after each revision. Nobody types it in. ONE statement for the
    batch; the reload that follows picks the value up (populate_existing).

    `unsourced` are the aircraft whose body did not say where the record came from. The default is
    `cirium`, which is wrong for an airframe entered by hand — so one Cirium did not answer for is
    filed as `manual` instead. A source the caller DID send is never second-guessed."""
    await session.execute(text("SELECT fleet.refresh_usage_status(CAST(:ids AS bigint[]))"),
                          {"ids": aircraft_ids})
    if unsourced:
        await session.execute(
            text("UPDATE fleet.service_info SET source = 'manual' "
                 "WHERE aircraft_id = ANY (CAST(:ids AS bigint[])) AND usage_status IS NULL"),
            {"ids": unsourced})


async def _aircraft_card(session, row: Aircraft, on: date, history: bool) -> dict:
    """Everything known about one airframe: identity, engines, the lease terms and the policy in
    force on `on`, and — unless `history=false` — every lease and every coverage it has ever had."""
    # The agreement's lessor and the policy's three parties are to-one all the way down, and the
    # models would fetch each with its own SELECT. Joined, a leased and insured aircraft costs the
    # same two trips here as an aircraft with neither — which is what the portal will meet once
    # contracts start being entered.
    leases = (await session.execute(
        select(AircraftLease).where(AircraftLease.aircraft_id == row.id)
        .options(joinedload(AircraftLease.agreement).joinedload(Agreement.lessor))
        .order_by(AircraftLease.effective_date.desc(), AircraftLease.id.desc())
    )).scalars().all()
    coverages = (await session.execute(
        select(Coverage).where(Coverage.aircraft_id == row.id)
        .options(*COVERAGE_LOAD)
        .order_by(Coverage.covered_from.desc(), Coverage.id.desc())
    )).scalars().all()

    current_lease = lease_in_force(leases, on)
    current_cover = next((c for c in coverages if covers(c, on)), None)
    out = aircraft_json(row)
    out["as_of"] = on.isoformat()
    out["lease"] = lease_json(current_lease, on=on, service=row.service)
    out["coverage"] = coverage_json(current_cover)
    if history:
        out["lease_history"] = [lease_json(l, on=on, service=row.service) for l in leases]
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
            row = await find_aircraft(session, registration=registration, msn=msn,
                                      options=_AIRCRAFT_ONE)
            if row is None:
                return warning_response(request=request, response=response,
                                        msg=f"No aircraft matches '{registration}'",
                                        status_code=status.HTTP_404_NOT_FOUND)
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
                select(Aircraft).where(Aircraft.id == aircraft_id).options(*_AIRCRAFT_ONE)
            )).unique().scalar_one_or_none()
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
                select(Aircraft).where(Aircraft.id == aircraft_id).options(*_AIRCRAFT_ONE)
            )).unique().scalar_one_or_none()
            if row is None:
                return warning_response(request=request, response=response,
                                        msg=f"Aircraft {aircraft_id} not found",
                                        status_code=status.HTTP_404_NOT_FOUND)
            if "aircraft_type" in fields or "aircraft_category" in fields:
                # Either name may be omitted, and what is omitted is taken from the type the
                # aircraft is on now — so `{"aircraft_category": "cargo"}` alone moves a converted
                # freighter to the cargo row of the SAME series rather than needing the series
                # spelled out again.
                held = row.aircraft_type
                series = fields.pop("aircraft_type", None) or (held.master_series if held else None)
                maker = fields.pop("manufacturer", None) or (held.manufacturer if held else None)
                # A series change with no category keeps the role it is in: re-typing an
                # aircraft should not quietly turn a freighter into a passenger aircraft.
                wanted = fields.pop("aircraft_category", None) or (held.category if held else None)
                ac_type = await get_or_create_aircraft_type(session, series, maker, wanted)
                row.aircraft_type_id = ac_type.id if ac_type else None
            fields.pop("manufacturer", None)
            fields.pop("aircraft_category", None)
            if "airline" in fields:
                airline = await get_or_create_airline(session, fields.pop("airline"))
                row.airline_id = airline.id if airline else None
            for key, value in fields.items():
                setattr(row, key, value.strip() if isinstance(value, str) else value)
            await session.flush()
            row = await reload_with(session, Aircraft, row.id, *_AIRCRAFT_ONE)
            data = aircraft_json(row)
        await invalidate(request, AIRCRAFT_TYPE, AIRLINE)
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
                select(Aircraft).where(Aircraft.id == aircraft_id).options(*_AIRCRAFT_ONE)
            )).unique().scalar_one_or_none()
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
# the service block
# ==============================================================================================

@router.get(path="/aircraft/{aircraft_id}/service",
            description="The aircraft's service block. Returns the defaults with "
                        "`recorded: false` when no row has been written yet.",
            responses=build_responses(include=_OK), dependencies=_READ)
async def get_service(request: Request, response: Response, aircraft_id: int):
    try:
        async with request.app.state.db_client.read_session(DB) as session:
            # ONE query answers both questions. "Does the aircraft exist" and "has it a service
            # row" used to be two, and the first was a full ORM load of the aircraft, which
            # dragged its type, airline, engines and engine models along: seven round trips to
            # read one 1:1 row. A LEFT JOIN separates the three cases by itself - no aircraft
            # (404), an aircraft with no service row (the defaults), or both.
            found = (await session.execute(
                select(Aircraft.id, ServiceInfo)
                .outerjoin(ServiceInfo, ServiceInfo.aircraft_id == Aircraft.id)
                .where(Aircraft.id == aircraft_id)
            )).first()
            if found is None:
                return warning_response(request=request, response=response,
                                        msg=f"Aircraft {aircraft_id} not found",
                                        status_code=status.HTTP_404_NOT_FOUND)
            data = service_json(found[1])
        return success_response(request=request, response=response, data=data)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.patch(
    path="/aircraft/{aircraft_id}/service",
    description=(
        "Change the service block. Only the fields sent are touched, and the row is created with "
        "the defaults if the aircraft has none. `status` and `usage_status` are refused with 422: "
        "the first follows the policy coverage in force today, the second comes from Cirium."
    ),
    responses=build_responses(include=_OK),
)
async def update_service(request: Request, response: Response, aircraft_id: int,
                         body: ServicePatch,
                         token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        fields = body.model_dump(exclude_unset=True)
        for key in ("lease_currency",):
            if fields.get(key):
                fields[key] = fields[key].upper()
        async with request.app.state.db_client.session(DB) as session:
            await set_actor(session, token)
            found = (await session.execute(
                select(Aircraft.id, ServiceInfo)
                .outerjoin(ServiceInfo, ServiceInfo.aircraft_id == Aircraft.id)
                .where(Aircraft.id == aircraft_id)
            )).first()
            if found is None:
                return warning_response(request=request, response=response,
                                        msg=f"Aircraft {aircraft_id} not found",
                                        status_code=status.HTTP_404_NOT_FOUND)
            row = found[1]
            if row is None:
                row = ServiceInfo(aircraft_id=aircraft_id, **fields)
                session.add(row)
                await session.flush()
                # a row that did not exist has only the column defaults for the two statuses;
                # set them from the coverage and Cirium like any other row's, then re-read
                await session.execute(
                    text("SELECT fleet.refresh_insurance_status(CAST(:ids AS bigint[])), "
                         "fleet.refresh_usage_status(CAST(:ids AS bigint[]))"),
                    {"ids": [aircraft_id]})
                await session.refresh(row)
            else:
                for key, value in fields.items():
                    setattr(row, key, value)
            await session.flush()
            data = service_json(row)
        return success_response(request=request, response=response, data=data)
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
                .options(*ENGINE_LOAD)
                .order_by(AircraftEngine.position,
                          AircraftEngine.installed_on.desc().nulls_last(),
                          AircraftEngine.id.desc())
            )).unique().scalars().all()
            # Serialized INSIDE the session. Outside it these rows are detached, and reading a
            # relationship off a detached instance raises rather than loading - which is right,
            # but it means the rendering has to happen while the session is still open.
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
            # An existence check selects the ID and nothing else: loading the aircraft would
            # fetch four relationships to answer a yes/no question.
            exists = (await session.execute(
                select(Aircraft.id).where(Aircraft.id == aircraft_id))).scalar_one_or_none()
            if exists is None:
                return warning_response(request=request, response=response,
                                        msg=f"Aircraft {aircraft_id} not found",
                                        status_code=status.HTTP_404_NOT_FOUND)
            row = AircraftEngine(aircraft_id=aircraft_id,
                                 **await _engine_fields(session, body))
            session.add(row)
            await session.flush()
            row = await reload_with(session, AircraftEngine, row.id,
                                    joinedload(AircraftEngine.engine_type))
            data = engine_json(row, fitted=True)
        await invalidate(request, ENGINE_TYPE)   # the model may have been created here
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
            row = await session.get(AircraftEngine, engine_id, options=ENGINE_LOAD)
            if row is None:
                return warning_response(request=request, response=response,
                                        msg=f"Engine {engine_id} not found",
                                        status_code=status.HTTP_404_NOT_FOUND)
            for key, value in (await _engine_fields(session, body, partial=True)).items():
                setattr(row, key, value)
            await session.flush()
            row = await reload_with(session, AircraftEngine, row.id,
                                    joinedload(AircraftEngine.engine_type))
            data = engine_json(row)
        await invalidate(request, ENGINE_TYPE)
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
            row = await session.get(AircraftEngine, engine_id, options=ENGINE_LOAD)
            if row is None:
                return warning_response(request=request, response=response,
                                        msg=f"Engine {engine_id} not found",
                                        status_code=status.HTTP_404_NOT_FOUND)
            data = engine_json(row)
            await session.delete(row)
        return success_response(request=request, response=response, data=data, msg="Engine deleted")
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)
