"""Forecast panel trigger endpoint.

POST /forecast/ — validate the operator against the Cirium fleet and enqueue the external-worker
`forecast_panel` job (build forecast.history_1 from Cirium × FR24, then merge into forecast.final_1).
Returns the job_id; the worker publishes a SEQUENTIAL status per step (validating -> preparing ->
FR24 check -> assembling -> merging -> done) which the portal reads from /status/{job_id} (poll) or
/status/stream (SSE). A `queued` status row is written here up front so the job is pollable the
instant this returns.
"""
import json
import uuid
from datetime import date as date_cls, datetime, timezone
from typing import Optional

from fastapi import Request, Response, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from Config import setup_logger
from settings import Router
from Queue import EXTERNAL_QUEUE
from Database import JobStatus
from api_auth import authorize, SCOPE_PREDICTIVE_WRITE
from Utils import success_response, error_response
from Utils.ResponsesFunc import build_responses, warning_response

logger = setup_logger("forecast_api")

router = Router(prefix="/forecast", tags=["Forecast"])

_REF = "forecast_panel"
_STATUS_CHANNEL = "status:events"   # must match the workers' status.py / StatusCheck.py


class ForecastRequest(BaseModel):
    operator: str = Field(..., description='Cirium "Operator" value, e.g. "Avianca"')
    date: Optional[date_cls] = Field(
        None,
        description="As-of date, ISO YYYY-MM-DD. History window = [2023-07-01, date); default today "
                    "(so it ends yesterday).",
    )


async def _mark_queued(request: Request, job_id: str, operator: str) -> None:
    """Insert a `queued` job_statuses row and publish it, so /status is populated immediately."""
    msg = f"Queued: forecast panel for '{operator}'"
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
    description="Start the forecast panel build (Cirium × FR24 → forecast.final_1) for an operator.",
    status_code=status.HTTP_202_ACCEPTED,
    responses=build_responses(include={
        status.HTTP_202_ACCEPTED, status.HTTP_404_NOT_FOUND,
        status.HTTP_422_UNPROCESSABLE_ENTITY, status.HTTP_500_INTERNAL_SERVER_ERROR,
    }),
    dependencies=[Depends(authorize(SCOPE_PREDICTIVE_WRITE))],
)
async def start_forecast(body: ForecastRequest, request: Request, response: Response):
    try:
        operator = body.operator.strip()

        # validate the operator exists in the Cirium fleet (fast EXISTS; index on "Operator")
        async with request.app.state.db_client.session("cirium") as session:
            found = (await session.execute(
                text('SELECT 1 FROM cirium.ciriumaircrafts WHERE "Operator" = :op LIMIT 1'),
                {"op": operator},
            )).first()
        if found is None:
            return warning_response(
                request=request, response=response,
                msg=f"Operator '{operator}' not found in cirium.ciriumaircrafts",
                status_code=status.HTTP_404_NOT_FOUND,
            )

        # own job_id so the queued row is written BEFORE the worker's first publish (no upsert race)
        job_id = uuid.uuid4().hex
        await _mark_queued(request, job_id, operator)

        await request.state.arq.enqueue_job(
            _REF,
            operator=operator,
            as_of=body.date.isoformat() if body.date else None,
            correlation_id=request.state.correlation_id,
            _job_id=job_id,
            _queue_name=EXTERNAL_QUEUE,
        )
        return success_response(
            request=request, response=response,
            data={"job_id": job_id, "operator": operator,
                  "as_of": body.date.isoformat() if body.date else None},
            msg="Forecast panel started", status_code=status.HTTP_202_ACCEPTED,
        )
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)
