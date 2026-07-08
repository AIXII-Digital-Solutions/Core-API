"""
Job status: REST + live stream.

Reads the durable `job_statuses` table (service DB), written by the worker segments
(file_processor / external_worker), and relays the live `status:events` Redis channel
they publish to as Server-Sent Events.
"""
from typing import Optional

from arq.jobs import Job
from fastapi import Request, Response, Query, status, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from Config import setup_logger
from settings import Router
from Queue import EXTERNAL_QUEUE
from Database import JobStatus
from api_auth import authorize
from Utils import success_response, warning_response

logger = setup_logger("status_api")

router = Router(prefix="/status", tags=["Status"])

STATUS_CHANNEL = "status:events"  # must match the workers' status.py
_CANCEL_KEY = "job:cancel:{}"     # must match the worker's panel.py cooperative-cancel flag
_TERMINAL = {"success", "error", "skipped", "cancelled"}


def _serialize(j: JobStatus) -> dict:
    return {
        "job_id": j.job_id,
        "kind": j.kind,
        "ref": j.ref,
        "state": j.state,
        "progress": j.progress,
        "message": j.message,
        "payload": j.payload,
        "created_at": j.created_at.isoformat() if j.created_at else None,
        "updated_at": j.updated_at.isoformat() if j.updated_at else None,
        "finished_at": j.finished_at.isoformat() if j.finished_at else None,
    }


@router.get("")
async def list_status(
    request: Request,
    response: Response,
    kind: Optional[str] = Query(None, description="file | external"),
    state: Optional[str] = Query(None, description="queued | running | success | error | skipped"),
    limit: int = Query(100, le=1000),
):
    stmt = select(JobStatus).order_by(JobStatus.updated_at.desc()).limit(limit)
    if kind:
        stmt = stmt.where(JobStatus.kind == kind)
    if state:
        stmt = stmt.where(JobStatus.state == state)
    async with request.app.state.db_client.session("service") as session:
        rows = (await session.execute(stmt)).scalars().all()
    return success_response(request=request, response=response, data=[_serialize(r) for r in rows])


@router.get("/stream")
async def stream_status(request: Request):
    """Server-Sent Events: live job-status updates published by the workers."""
    redis = request.app.state.redis

    async def event_gen():
        pubsub = redis.pubsub()
        await pubsub.subscribe(STATUS_CHANNEL)
        try:
            async for msg in pubsub.listen():
                if msg.get("type") != "message":
                    continue
                data = msg["data"]
                if isinstance(data, bytes):
                    data = data.decode()
                yield f"data: {data}\n\n"
        finally:
            await pubsub.unsubscribe(STATUS_CHANNEL)
            await pubsub.aclose()

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@router.get("/{job_id}")
async def get_status(job_id: str, request: Request, response: Response):
    async with request.app.state.db_client.session("service") as session:
        row = (
            await session.execute(select(JobStatus).where(JobStatus.job_id == job_id))
        ).scalar_one_or_none()
    if row is None:
        return warning_response(
            request=request, response=response,
            msg=f"No job with id '{job_id}'",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    return success_response(request=request, response=response, data=_serialize(row))


@router.post("/{job_id}/cancel", dependencies=[Depends(authorize())])
async def cancel_status(job_id: str, request: Request, response: Response):
    """Cancel a running job. Sets a cooperative Redis flag (checked by the worker's forecast/fetch loop,
    which then stops with a terminal `cancelled` status) AND sends a generic ARQ abort so ANY job's
    running task is cancelled. Idempotent: a job that already finished returns its state unchanged."""
    async with request.app.state.db_client.session("service") as session:
        row = (
            await session.execute(select(JobStatus).where(JobStatus.job_id == job_id))
        ).scalar_one_or_none()
    if row is None:
        return warning_response(request=request, response=response,
                                msg=f"No job with id '{job_id}'",
                                status_code=status.HTTP_404_NOT_FOUND)
    if row.state in _TERMINAL:
        return success_response(request=request, response=response,
                                data={"job_id": job_id, "state": row.state},
                                msg="Job already finished")
    # 1) cooperative flag — the worker checks it and stops cleanly with a `cancelled` status
    try:
        await request.app.state.redis.set(_CANCEL_KEY.format(job_id), "1", ex=3600)
    except Exception:
        logger.exception("failed to set cancel flag for %s", job_id)
    # 2) generic ARQ abort — cancels the running task for ANY job (backstop / non-cooperative jobs)
    aborted = False
    try:
        job = Job(job_id, redis=request.state.arq, _queue_name=EXTERNAL_QUEUE)
        aborted = await job.abort(timeout=2)
    except Exception as _ex:
        logger.warning("arq abort for %s failed: %s", job_id, _ex)
    return success_response(request=request, response=response,
                            data={"job_id": job_id, "aborted": aborted},
                            msg="Cancellation requested")
