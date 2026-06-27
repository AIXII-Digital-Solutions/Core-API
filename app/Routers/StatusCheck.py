"""
Job status: REST + live stream.

Reads the durable `job_statuses` table (service DB), written by the worker segments
(file_processor / external_worker), and relays the live `status:events` Redis channel
they publish to as Server-Sent Events.
"""
from typing import Optional

from fastapi import Request, Response, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from settings import Router
from Database import JobStatus
from Utils import success_response, warning_response

router = Router(prefix="/status", tags=["Status"])

STATUS_CHANNEL = "status:events"  # must match the workers' status.py


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
