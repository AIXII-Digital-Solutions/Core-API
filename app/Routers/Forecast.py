"""Forecast panel trigger + last-request read-back.

POST /forecast/       — validate (operator and/or registrations) and enqueue the external-worker
                        `forecast_panel` job (build forecast.acys_actuals from Cirium × FR24, merge
                        into forecast.acys_summary). Records the request in
                        service.forecast_last_requests and returns the job_id. The worker publishes a
                        SEQUENTIAL status per step, read from /status/{job_id} (poll) or
                        /status/stream (SSE).
GET  /forecast/last   — the most recent trigger (datetime + request_type + params).

acys_actuals accumulates across requests (this operator/tail slice is refreshed);
acys_forecast/acys_summary are per-request. A `queued` status row is written up front so the job is
pollable immediately.
"""
import json
import uuid
from datetime import date as date_cls
from typing import Optional, List

from fastapi import Request, Response, Depends, Query, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select, text

from Config import setup_logger
from settings import Router
from Queue import EXTERNAL_QUEUE
from Database import JobStatus
from Database.ServiceModels import ForecastLastRequest
from api_auth import authorize, SCOPE_PREDICTIVE_WRITE, SCOPE_PREDICTIVE_READ
from Utils import success_response, error_response
from Utils.ResponsesFunc import build_responses, warning_response

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

        # record the request for /forecast/last read-back
        async with request.app.state.db_client.session("service") as session:
            session.add(ForecastLastRequest(
                request_type=_REQUEST_TYPE,
                request_params={"operator": operator, "registrations": registrations, "date": as_of},
            ))
            await session.commit()

        await request.state.arq.enqueue_job(
            _REF,
            operator=operator,
            registrations=registrations,
            as_of=as_of,
            correlation_id=request.state.correlation_id,
            _job_id=job_id,
            _queue_name=EXTERNAL_QUEUE,
        )
        return success_response(
            request=request, response=response,
            data={"job_id": job_id, "operator": operator, "registrations": registrations, "as_of": as_of},
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
