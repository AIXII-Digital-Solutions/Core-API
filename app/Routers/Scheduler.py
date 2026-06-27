"""
Scheduler control plane (admin API).

Reads/edits the ``schedule_registry`` table that drives the workers' dispatcher: list
jobs, enable/disable, pause/resume, change the interval or cron expression, edit kwargs,
and force an immediate run. core-api OWNS this table; the workers seed default rows and
re-read it on every dispatcher tick, so changes take effect at runtime (within ~1 minute)
with no worker restart — for every existing and future schedulable job.

Protected by the service token for now; a dedicated API-token scope gates it later.
"""
from typing import Optional, List

from fastapi import Request, Response, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from settings import Router
from Config import setup_logger
from Database import ScheduleEntry
from Schemas import DefaultResponse
from Utils import success_response, warning_response, error_response
from Utils.ResponsesFunc import build_responses
from api_auth import authorize, SCOPE_SCHEDULER_READ, SCOPE_SCHEDULER_WRITE

logger = setup_logger("scheduler_api")

router = Router(prefix="/scheduler", tags=["Scheduler"])


class ScheduleOut(BaseModel):
    name: str
    queue: str
    func_name: str
    kwargs: Optional[dict] = None
    interval_seconds: Optional[int] = None
    cron_expr: Optional[str] = None
    enabled: bool
    paused: bool
    run_now: bool
    next_run_at: Optional[str] = None
    last_run_at: Optional[str] = None
    last_status: Optional[str] = None
    description: Optional[str] = None


class SchedulePatch(BaseModel):
    """All fields optional — only the provided ones are updated.

    Setting ``interval_seconds`` clears ``cron_expr`` and vice-versa (a schedule is
    interval-driven XOR cron-driven).
    """
    enabled: Optional[bool] = None
    paused: Optional[bool] = None
    interval_seconds: Optional[int] = Field(default=None, ge=1)
    cron_expr: Optional[str] = None
    kwargs: Optional[dict] = None
    description: Optional[str] = None


def _serialize(s: ScheduleEntry) -> dict:
    return {
        "name": s.name,
        "queue": s.queue,
        "func_name": s.func_name,
        "kwargs": s.kwargs,
        "interval_seconds": s.interval_seconds,
        "cron_expr": s.cron_expr,
        "enabled": s.enabled,
        "paused": s.paused,
        "run_now": s.run_now,
        "next_run_at": s.next_run_at.isoformat() if s.next_run_at else None,
        "last_run_at": s.last_run_at.isoformat() if s.last_run_at else None,
        "last_status": s.last_status,
        "description": s.description,
    }


@router.get(
    "",
    response_model=DefaultResponse[List[ScheduleOut]],
    responses=build_responses(include={status.HTTP_200_OK, status.HTTP_500_INTERNAL_SERVER_ERROR}),
    dependencies=[Depends(authorize(SCOPE_SCHEDULER_READ))],
)
async def list_schedules(request: Request, response: Response):
    """List every schedule and its current state."""
    try:
        async with request.app.state.db_client.session("service") as session:
            rows = (await session.execute(select(ScheduleEntry).order_by(ScheduleEntry.name))).scalars().all()
        return success_response(request=request, response=response, data=[_serialize(r) for r in rows])
    except Exception as ex:
        logger.error(f"list_schedules failed: {ex}")
        return error_response(request=request, response=response, exc=ex)


@router.get(
    "/{name}",
    responses=build_responses(include={status.HTTP_200_OK, status.HTTP_404_NOT_FOUND, status.HTTP_500_INTERNAL_SERVER_ERROR}),
    dependencies=[Depends(authorize(SCOPE_SCHEDULER_READ))],
)
async def get_schedule(name: str, request: Request, response: Response):
    try:
        async with request.app.state.db_client.session("service") as session:
            row = (await session.execute(select(ScheduleEntry).where(ScheduleEntry.name == name))).scalar_one_or_none()
        if row is None:
            return warning_response(request=request, response=response,
                                    msg=f"No schedule named '{name}'", status_code=status.HTTP_404_NOT_FOUND)
        return success_response(request=request, response=response, data=_serialize(row))
    except Exception as ex:
        logger.error(f"get_schedule failed: {ex}")
        return error_response(request=request, response=response, exc=ex)


@router.patch(
    "/{name}",
    responses=build_responses(include={status.HTTP_200_OK, status.HTTP_404_NOT_FOUND, status.HTTP_400_BAD_REQUEST, status.HTTP_500_INTERNAL_SERVER_ERROR}),
    dependencies=[Depends(authorize(SCOPE_SCHEDULER_WRITE))],
)
async def patch_schedule(name: str, body: SchedulePatch, request: Request, response: Response):
    """Update a schedule. Takes effect at the next dispatcher tick (~1 min)."""
    try:
        async with request.app.state.db_client.session("service") as session:
            row = (await session.execute(select(ScheduleEntry).where(ScheduleEntry.name == name))).scalar_one_or_none()
            if row is None:
                return warning_response(request=request, response=response,
                                        msg=f"No schedule named '{name}'", status_code=status.HTTP_404_NOT_FOUND)

            if body.enabled is not None:
                row.enabled = body.enabled
            if body.paused is not None:
                row.paused = body.paused
            if body.kwargs is not None:
                row.kwargs = body.kwargs
            if body.description is not None:
                row.description = body.description
            # interval XOR cron — setting one clears the other
            if body.interval_seconds is not None:
                row.interval_seconds = body.interval_seconds
                row.cron_expr = None
            elif body.cron_expr is not None:
                row.cron_expr = body.cron_expr
                row.interval_seconds = None
            # session commits on exit
            data = _serialize(row)
        return success_response(request=request, response=response, data=data, msg="Schedule updated")
    except Exception as ex:
        logger.error(f"patch_schedule failed: {ex}")
        return error_response(request=request, response=response, exc=ex)


@router.post(
    "/{name}/run",
    responses=build_responses(include={status.HTTP_202_ACCEPTED, status.HTTP_404_NOT_FOUND, status.HTTP_500_INTERNAL_SERVER_ERROR}),
    dependencies=[Depends(authorize(SCOPE_SCHEDULER_WRITE))],
)
async def run_schedule_now(name: str, request: Request, response: Response):
    """Force an immediate run: enqueue the job on its queue right now (instant).

    Independent of enabled/paused/next_run_at — observe progress via /status/{job_id}.
    """
    try:
        async with request.app.state.db_client.session("service") as session:
            row = (await session.execute(select(ScheduleEntry).where(ScheduleEntry.name == name))).scalar_one_or_none()
            if row is None:
                return warning_response(request=request, response=response,
                                        msg=f"No schedule named '{name}'", status_code=status.HTTP_404_NOT_FOUND)
            func_name, queue = row.func_name, row.queue
            # strip arq-reserved (_-prefixed) keys so an operator-edited kwargs blob can't
            # collide with _queue_name/_job_id/etc.
            kwargs = {k: v for k, v in (row.kwargs or {}).items() if not str(k).startswith("_")}

        job = await request.state.arq.enqueue_job(func_name, _queue_name=queue, **kwargs)
        job_id = getattr(job, "job_id", None)
        return success_response(
            request=request, response=response,
            data={"name": name, "func_name": func_name, "queue": queue, "job_id": job_id},
            msg="Run enqueued", status_code=status.HTTP_202_ACCEPTED,
        )
    except Exception as ex:
        logger.error(f"run_schedule_now failed: {ex}")
        return error_response(request=request, response=response, exc=ex)
