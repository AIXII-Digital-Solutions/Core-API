"""Forecast panel trigger + last-request read-back + the model's tuning profiles.

POST /forecast/       — validate (operator and/or registrations) and enqueue the external-worker
                        `forecast_panel` job (build forecast.acys_actuals from Cirium × FR24, merge
                        into forecast.acys_summary_by_day). Returns the job_id. The worker publishes a
                        SEQUENTIAL status per step, read from /status/{job_id} (poll) or
                        /status/stream (SSE), and writes the service.forecast_last_requests row ONLY
                        after the whole panel finishes successfully (not at trigger time).
GET  /forecast/last   — the most recent trigger (datetime + request_type + params).

TUNING PROFILES (service.forecast_profiles) — what the portal's settings screen talks to:
GET    /forecast/params/schema  — the FORM DESCRIPTOR: every knob with type/bounds/default/label/
                                  description/group. The portal renders its form from THIS, so adding a
                                  knob never needs a portal release.
GET    /forecast/profiles       — list; POST — create; PATCH /{name} — edit; DELETE /{name} — remove;
POST   /forecast/profiles/{name}/default — make it the profile used when a run names none.

A profile's `params` holds OVERRIDES ONLY (absent key = the spec's default), and every write is resolved
through `Utils.forecast_params.resolve` before it is stored — so an invalid knob is rejected at SAVE time,
with a message naming the offending field, instead of surfacing as a broken forecast an hour later.
external-worker resolves again on read (the row is reachable outside this API).

acys_actuals accumulates across requests (this operator/tail slice is refreshed);
acys_forecast/acys_summary are per-request. A `queued` status row is written up front so the job is
pollable immediately.
"""
import json
import uuid
from datetime import date as date_cls
from typing import Optional, List, Dict, Any

from fastapi import Request, Response, Depends, Query, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select, text

from Config import setup_logger
from settings import Router
from Queue import EXTERNAL_QUEUE
from Database import JobStatus
from Database.ServiceModels import ForecastLastRequest, ForecastProfile
from api_auth import authorize, SCOPE_PREDICTIVE_WRITE, SCOPE_PREDICTIVE_READ
from Utils import success_response, error_response
from Utils.ResponsesFunc import build_responses, warning_response
from Utils.forecast_params import (MODEL_VERSION, ForecastParamError, describe, resolve)

logger = setup_logger("forecast_api")

router = Router(prefix="/forecast", tags=["Forecast"])

_REF = "forecast_panel"
_REQUEST_TYPE = "ACYS"              # current Cirium×FR24 panel algorithm
_STATUS_CHANNEL = "status:events"   # must match the workers' status.py / StatusCheck.py


class ForecastRequest(BaseModel):
    operator: Optional[str] = Field(None, description='Cirium "Operator" value, e.g. "Avianca".')
    registrations: Optional[List[str]] = Field(
        None, description="One or more Cirium registrations. May be used alone OR together with "
                          "operator (the scope is the union).")
    date: Optional[date_cls] = Field(
        None, description="As-of date YYYY-MM-DD; history window = [2023-07-01, date), default today. "
                          "Also the Contract Year anchor.")
    profile: Optional[str] = Field(
        None, description="Name of a service.forecast_profiles tuning profile. Omitted, the default "
                          "profile is used. A named profile that does not exist fails the run rather "
                          "than silently falling back.")

    @model_validator(mode="after")
    def _at_least_one_mode(self):
        has_op = bool(self.operator and self.operator.strip())
        has_regs = bool(self.registrations)
        if not has_op and not has_regs:
            raise ValueError("provide operator and/or registrations")
        return self


async def _mark_queued(request: Request, job_id: str, label: str) -> None:
    """Insert a `queued` job_statuses row and publish it, so /status is populated immediately."""
    msg = f"Queued: forecast panel for {label}"
    async with request.app.state.db_client.session("service") as session:
        session.add(JobStatus(job_id=job_id, kind="external", ref=_REF,
                              state="queued", progress=0, message=msg))
        await session.commit()
    try:
        await request.app.state.redis.publish(_STATUS_CHANNEL, json.dumps({
            "job_id": job_id, "kind": "external", "ref": _REF,
            "state": "queued", "progress": 0, "message": msg,
        }))
    except Exception:
        pass


@router.post(
    path="/",
    description="Start the forecast panel build (Cirium × FR24 → forecast.acys_summary_by_day; "
                "grouped rollup in the forecast.acys_summary_grouped view) for an operator and/or "
                "a list of registrations.",
    status_code=status.HTTP_202_ACCEPTED,
    responses=build_responses(include={
        status.HTTP_202_ACCEPTED, status.HTTP_404_NOT_FOUND,
        status.HTTP_422_UNPROCESSABLE_ENTITY, status.HTTP_500_INTERNAL_SERVER_ERROR,
    }),
    dependencies=[Depends(authorize(SCOPE_PREDICTIVE_WRITE))],
)
async def start_forecast(body: ForecastRequest, request: Request, response: Response):
    try:
        operator = body.operator.strip() if body.operator else None
        registrations = [r.strip() for r in body.registrations if r and r.strip()] if body.registrations else None
        registrations = registrations or None

        # validate the COMBINED scope (operator's tails OR the explicit regs) matches something
        clauses, params = [], {}
        if operator:
            clauses.append('"Operator" = :op'); params["op"] = operator
        if registrations:
            clauses.append('"Registration" = ANY(:regs)'); params["regs"] = registrations
        where = "(" + " OR ".join(clauses) + ")"
        async with request.app.state.db_client.session("cirium") as session:
            found = (await session.execute(
                text(f'SELECT 1 FROM cirium.ciriumaircrafts WHERE {where} LIMIT 1'), params)).first()
        if found is None:
            return warning_response(request=request, response=response,
                                    msg="No Cirium aircraft match the given operator / registrations",
                                    status_code=status.HTTP_404_NOT_FOUND)

        label = " + ".join(([f"operator '{operator}'"] if operator else [])
                           + ([f"{len(registrations)} registration(s)"] if registrations else []))
        as_of = body.date.isoformat() if body.date else None

        # own job_id so the queued row is written BEFORE the worker's first publish (no upsert race)
        job_id = uuid.uuid4().hex
        await _mark_queued(request, job_id, label)

        # NOTE: the forecast_last_requests row is written by the WORKER, and ONLY after the whole
        # panel finishes successfully — not here at trigger time (a failed/cancelled run leaves no row).

        await request.state.arq.enqueue_job(
            _REF,
            operator=operator,
            registrations=registrations,
            as_of=as_of,
            profile=body.profile,
            correlation_id=request.state.correlation_id,
            _job_id=job_id,
            _queue_name=EXTERNAL_QUEUE,
        )
        return success_response(
            request=request, response=response,
            data={"job_id": job_id, "operator": operator, "registrations": registrations,
                  "as_of": as_of, "profile": body.profile},
            msg="Forecast panel started", status_code=status.HTTP_202_ACCEPTED,
        )
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.get(
    path="/last",
    description="The most recent POST /forecast/ trigger (datetime + request_type + params). "
                "Optionally filter by request_type.",
    responses=build_responses(include={status.HTTP_200_OK, status.HTTP_404_NOT_FOUND,
                                        status.HTTP_500_INTERNAL_SERVER_ERROR}),
    dependencies=[Depends(authorize(SCOPE_PREDICTIVE_READ))],
)
async def last_forecast(
    request: Request, response: Response,
    request_type: Optional[str] = Query(None, description="Filter by request_type (e.g. 'ACYS')."),
):
    try:
        stmt = select(ForecastLastRequest).order_by(ForecastLastRequest.created_at.desc()).limit(1)
        if request_type:
            stmt = stmt.where(ForecastLastRequest.request_type == request_type)
        async with request.app.state.db_client.session("service") as session:
            row = (await session.execute(stmt)).scalars().first()
        if row is None:
            return warning_response(request=request, response=response,
                                    msg="No forecast request recorded yet",
                                    status_code=status.HTTP_404_NOT_FOUND)
        return success_response(request=request, response=response, data={
            "datetime": row.created_at.isoformat() if row.created_at else None,
            "request_type": row.request_type,
            "request_params": row.request_params,
        })
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


# ── Tuning profiles ────────────────────────────────────────────────────────────────────────────────────
# The knobs that used to be constants in external-worker's ForecastAPI/model.py. The portal edits them
# here; the worker reads the profile at the start of each run. Utils/forecast_params.py holds the spec
# (defaults, types, bounds, labels); this router only stores and validates against it.

class ProfileIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=128,
                      description="Unique identifier, e.g. 'default' or 'aggressive-seasonality'.")
    description: Optional[str] = Field(None, description="Free text: what this profile is for.")
    params: Dict[str, Any] = Field(
        default_factory=dict,
        description="OVERRIDES ONLY — an absent knob keeps its default. Validated against "
                    "GET /forecast/params/schema; an unknown key or an out-of-range value is a 422.")
    is_default: bool = Field(False, description="Use this profile when a run names none. At most one.")
    enabled: bool = Field(True, description="A disabled profile cannot be used by a run.")


class ProfilePatch(BaseModel):
    """Every field optional — only what is sent changes. `params` REPLACES the override set rather than
    merging: with a merge there would be no way to REMOVE an override, since an absent key already means
    'use the default'."""
    description: Optional[str] = None
    params: Optional[Dict[str, Any]] = None
    is_default: Optional[bool] = None
    enabled: Optional[bool] = None


def _profile_out(row: ForecastProfile) -> dict:
    """One profile as the portal sees it: the stored overrides AND the resolved effective values, so the
    form can show "what is set here" beside "what a run will actually use" without re-implementing the
    defaulting rules in the frontend."""
    try:
        effective = resolve(row.params, model_version=row.model_version)
        error = None
    except ForecastParamError as e:
        # A row stored before a MODEL_VERSION bump, or hand-edited in the database, must stay VISIBLE —
        # that is how it gets fixed. Report the problem per-row instead of failing the whole listing.
        effective, error = None, str(e)
    return {
        "name": row.name,
        "description": row.description,
        "model_version": row.model_version,
        "params": row.params or {},
        "effective": {k: (v.isoformat() if hasattr(v, "isoformat") else v)
                      for k, v in effective.items()} if effective else None,
        "error": error,
        "is_default": row.is_default,
        "enabled": row.enabled,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "updated_by": row.updated_by,
    }


async def _clear_default(session, *, keep_name: Optional[str] = None) -> None:
    """Drop is_default from every OTHER profile. At most one default is a DATABASE guarantee (a partial
    unique index), so this must run in the SAME transaction as the SET that follows or the index rejects
    the write."""
    stmt = text("UPDATE forecast_profiles SET is_default = false WHERE is_default"
                + (" AND name <> :n" if keep_name else ""))
    await session.execute(stmt, {"n": keep_name} if keep_name else {})


@router.get(
    path="/params/schema",
    description="The forecast model's tunable parameters: type, bounds, default, label, description and "
                "form group for each. The portal renders its settings form from this, so adding a knob "
                "needs no portal release.",
    responses=build_responses(include={status.HTTP_200_OK, status.HTTP_500_INTERNAL_SERVER_ERROR}),
    dependencies=[Depends(authorize(SCOPE_PREDICTIVE_READ))],
)
async def params_schema(request: Request, response: Response):
    try:
        return success_response(request=request, response=response, data=describe())
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.get(
    path="/profiles",
    description="All tuning profiles, each with its stored overrides and its resolved effective values.",
    responses=build_responses(include={status.HTTP_200_OK, status.HTTP_500_INTERNAL_SERVER_ERROR}),
    dependencies=[Depends(authorize(SCOPE_PREDICTIVE_READ))],
)
async def list_profiles(request: Request, response: Response):
    try:
        async with request.app.state.db_client.session("service") as session:
            rows = (await session.execute(
                select(ForecastProfile).order_by(ForecastProfile.is_default.desc(),
                                                 ForecastProfile.name))).scalars().all()
            data = [_profile_out(r) for r in rows]
        return success_response(request=request, response=response, data=data)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.post(
    path="/profiles",
    description="Create a tuning profile. `params` holds overrides only and is validated against "
                "/forecast/params/schema.",
    status_code=status.HTTP_201_CREATED,
    responses=build_responses(include={status.HTTP_201_CREATED, status.HTTP_409_CONFLICT,
                                       status.HTTP_422_UNPROCESSABLE_ENTITY,
                                       status.HTTP_500_INTERNAL_SERVER_ERROR}),
    dependencies=[Depends(authorize(SCOPE_PREDICTIVE_WRITE))],
)
async def create_profile(body: ProfileIn, request: Request, response: Response):
    try:
        try:
            resolve(body.params)          # reject bad knobs BEFORE anything is stored
        except ForecastParamError as e:
            return warning_response(request=request, response=response, msg=str(e),
                                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY)
        async with request.app.state.db_client.session("service") as session:
            exists = (await session.execute(
                select(ForecastProfile).where(ForecastProfile.name == body.name))).scalars().first()
            if exists is not None:
                return warning_response(request=request, response=response,
                                        msg=f"Profile '{body.name}' already exists",
                                        status_code=status.HTTP_409_CONFLICT)
            if body.is_default:
                await _clear_default(session)
            row = ForecastProfile(name=body.name, description=body.description,
                                  model_version=MODEL_VERSION, params=body.params,
                                  is_default=body.is_default, enabled=body.enabled,
                                  updated_by=getattr(request.state, "caller", None))
            session.add(row)
            await session.commit()
            await session.refresh(row)
            data = _profile_out(row)
        return success_response(request=request, response=response, data=data,
                                msg=f"Profile '{body.name}' created",
                                status_code=status.HTTP_201_CREATED)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.patch(
    path="/profiles/{name}",
    description="Edit a profile. Only the fields sent change. `params` REPLACES the override set — it is "
                "not merged, so an override is removed by omitting it.",
    responses=build_responses(include={status.HTTP_200_OK, status.HTTP_404_NOT_FOUND,
                                       status.HTTP_422_UNPROCESSABLE_ENTITY,
                                       status.HTTP_500_INTERNAL_SERVER_ERROR}),
    dependencies=[Depends(authorize(SCOPE_PREDICTIVE_WRITE))],
)
async def update_profile(name: str, body: ProfilePatch, request: Request, response: Response):
    try:
        if body.params is not None:
            try:
                resolve(body.params)
            except ForecastParamError as e:
                return warning_response(request=request, response=response, msg=str(e),
                                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY)
        async with request.app.state.db_client.session("service") as session:
            row = (await session.execute(
                select(ForecastProfile).where(ForecastProfile.name == name))).scalars().first()
            if row is None:
                return warning_response(request=request, response=response,
                                        msg=f"Profile '{name}' not found",
                                        status_code=status.HTTP_404_NOT_FOUND)
            if body.description is not None:
                row.description = body.description
            if body.params is not None:
                row.params = body.params
                # the overrides are now written in TODAY's vocabulary — record which one
                row.model_version = MODEL_VERSION
            if body.enabled is not None:
                row.enabled = body.enabled
            if body.is_default is not None:
                if body.is_default:
                    await _clear_default(session, keep_name=name)
                row.is_default = body.is_default
            row.updated_by = getattr(request.state, "caller", None)
            await session.commit()
            await session.refresh(row)
            data = _profile_out(row)
        return success_response(request=request, response=response, data=data,
                                msg=f"Profile '{name}' updated")
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.post(
    path="/profiles/{name}/default",
    description="Make this the profile used by runs that name none. Clears the flag from the previous "
                "default in the same transaction.",
    responses=build_responses(include={status.HTTP_200_OK, status.HTTP_404_NOT_FOUND,
                                       status.HTTP_422_UNPROCESSABLE_ENTITY,
                                       status.HTTP_500_INTERNAL_SERVER_ERROR}),
    dependencies=[Depends(authorize(SCOPE_PREDICTIVE_WRITE))],
)
async def set_default_profile(name: str, request: Request, response: Response):
    try:
        async with request.app.state.db_client.session("service") as session:
            row = (await session.execute(
                select(ForecastProfile).where(ForecastProfile.name == name))).scalars().first()
            if row is None:
                return warning_response(request=request, response=response,
                                        msg=f"Profile '{name}' not found",
                                        status_code=status.HTTP_404_NOT_FOUND)
            if not row.enabled:
                return warning_response(request=request, response=response,
                                        msg=f"Profile '{name}' is disabled and cannot be the default",
                                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY)
            await _clear_default(session, keep_name=name)
            row.is_default = True
            row.updated_by = getattr(request.state, "caller", None)
            await session.commit()
            await session.refresh(row)
            data = _profile_out(row)
        return success_response(request=request, response=response, data=data,
                                msg=f"Profile '{name}' is now the default")
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.delete(
    path="/profiles/{name}",
    description="Delete a profile. The current default cannot be deleted — promote another one first, so "
                "a run can never be left with no profile to read.",
    responses=build_responses(include={status.HTTP_200_OK, status.HTTP_404_NOT_FOUND,
                                       status.HTTP_422_UNPROCESSABLE_ENTITY,
                                       status.HTTP_500_INTERNAL_SERVER_ERROR}),
    dependencies=[Depends(authorize(SCOPE_PREDICTIVE_WRITE))],
)
async def delete_profile(name: str, request: Request, response: Response):
    try:
        async with request.app.state.db_client.session("service") as session:
            row = (await session.execute(
                select(ForecastProfile).where(ForecastProfile.name == name))).scalars().first()
            if row is None:
                return warning_response(request=request, response=response,
                                        msg=f"Profile '{name}' not found",
                                        status_code=status.HTTP_404_NOT_FOUND)
            if row.is_default:
                return warning_response(
                    request=request, response=response,
                    msg=f"Profile '{name}' is the default; make another profile the default first",
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY)
            await session.delete(row)
            await session.commit()
        return success_response(request=request, response=response, data={"name": name},
                                msg=f"Profile '{name}' deleted")
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)
