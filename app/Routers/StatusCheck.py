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
from Queue import EXTERNAL_QUEUE, FILE_QUEUE
from Database import JobStatus
from api_auth import authorize, SCOPE_STATUS_READ, SCOPE_QUEUES_ADMIN
from Utils import success_response, warning_response

logger = setup_logger("status_api")

# `status:read` was defined in api_auth.py and wired to nothing, so every one of these routes was
# open: an anonymous caller could list every job with its `ref` — which for a file job is the
# absolute path of the uploaded file — plus its message and payload. The scope existed; it is used
# now. Cancelling is not a read, so it asks for more (see the route).
router = Router(prefix="/status", tags=["Status"],
                dependencies=[Depends(authorize(SCOPE_STATUS_READ))])

STATUS_CHANNEL = "status:events"  # must match the workers' status.py
_CANCEL_KEY = "job:cancel:{}"     # must match the worker's panel.py cooperative-cancel flag
_TERMINAL = {"success", "error", "skipped", "cancelled"}
# SSE keepalive: emit a comment at least this often even with no status events, so an idle stretch never
# trips a reverse-proxy read timeout (nginx proxy_read_timeout defaults to 60s) and drops the stream.
_SSE_KEEPALIVE_SECONDS = 15


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
    # ge=1: without a lower bound `limit=-1` renders LIMIT -1, which Postgres refuses, and the
    # handler has no try/except — a 500 for what is plainly a bad request.
    limit: int = Query(100, ge=1, le=1000),
):
    stmt = select(JobStatus).order_by(JobStatus.updated_at.desc()).limit(limit)
    if kind:
        stmt = stmt.where(JobStatus.kind == kind)
    if state:
        stmt = stmt.where(JobStatus.state == state)
    async with request.app.state.db_client.read_session("service") as session:
        rows = (await session.execute(stmt)).scalars().all()
    return success_response(request=request, response=response, data=[_serialize(r) for r in rows])


@router.get("/stream")
async def stream_status(request: Request):
    """Server-Sent Events: live job-status updates published by the workers.

    Robust against reverse-proxy idle timeouts: emits a `: ping` comment every _SSE_KEEPALIVE_SECONDS
    even when no status event is flowing (between jobs, or during a quiet step), so nginx never sees the
    stream go silent and never drops the upstream. `X-Accel-Buffering: no` disables nginx buffering so
    each event (and ping) reaches the client immediately. The loop also stops promptly on client
    disconnect. The channel is global; consumers filter by `job_id` client-side."""
    redis = request.app.state.redis

    async def event_gen():
        # Both INSIDE the try. Subscribing can fail on a Redis blip, and outside the try the
        # `finally` never runs — every failed connect would leak a pubsub connection.
        pubsub = None
        try:
            pubsub = redis.pubsub()
            await pubsub.subscribe(STATUS_CHANNEL)
            yield ": connected\n\n"   # first bytes immediately so the stream is established
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await pubsub.get_message(ignore_subscribe_messages=True,
                                                   timeout=_SSE_KEEPALIVE_SECONDS)
                except Exception as _ex:      # transient pubsub read error — ping and retry
                    logger.warning("status stream read error: %s", _ex)
                    yield ": ping\n\n"
                    continue
                if msg is None:               # no event within the window -> keepalive
                    yield ": ping\n\n"
                    continue
                if msg.get("type") != "message":
                    continue
                data = msg["data"]
                if isinstance(data, bytes):
                    data = data.decode()
                yield f"data: {data}\n\n"
        finally:
            try:
                if pubsub is not None:
                    await pubsub.unsubscribe(STATUS_CHANNEL)
                    await pubsub.aclose()
            except Exception:
                pass

    return StreamingResponse(
        event_gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                 "X-Accel-Buffering": "no"},
    )


@router.get("/{job_id}")
async def get_status(job_id: str, request: Request, response: Response):
    async with request.app.state.db_client.read_session("service") as session:
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


# `authorize()` with no scope required NOTHING: `set().issubset(anything)` is always true, so a key
# minted with only `flights:read` could kill a running forecast. Cancelling a job is an operational
# action and asks for the operational scope; the master service token satisfies it as it does
# everything else.
@router.post("/{job_id}/cancel", dependencies=[Depends(authorize(SCOPE_QUEUES_ADMIN))])
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
        # The queue the job is ON. Hardcoding the external queue meant cancelling a FILE job
        # aborted nothing while answering "Cancellation requested" — a success-shaped reply for
        # work that carried on running.
        queue = FILE_QUEUE if row.kind == "file" else EXTERNAL_QUEUE
        job = Job(job_id, redis=request.state.arq, _queue_name=queue)
        aborted = await job.abort(timeout=2)
    except Exception as _ex:
        logger.warning("arq abort for %s failed: %s", job_id, _ex)
    return success_response(request=request, response=response,
                            data={"job_id": job_id, "aborted": aborted},
                            msg="Cancellation requested")
