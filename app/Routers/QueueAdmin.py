"""
Queue admin (ARQ queues).

Inspect depth, pause/resume, and purge the ARQ queues this gateway feeds
(``core:external`` for external-worker, ``core:files`` reserved). Pause sets a Redis flag
(``queue:paused:<name>``) that the external-worker dispatcher honours before enqueuing
scheduled jobs — it is cooperative, so in-flight jobs are not killed. Purge drops every
queued (not-yet-started) job.

file-processor's own ``fp:process`` Redis-Streams queue is managed inside that service.

Protected by the service token for now; a dedicated API-token scope gates it later.
"""
from fastapi import Request, Response, Depends, status

from settings import Router
from Config import setup_logger
from Queue import EXTERNAL_QUEUE, FILE_QUEUE
from Schemas import DefaultResponse
from Utils import success_response, warning_response, error_response
from Utils.ResponsesFunc import build_responses
from api_auth import authorize, SCOPE_QUEUES_ADMIN

logger = setup_logger("queues_api")

router = Router(
    prefix="/queues",
    tags=["Queues"],
    dependencies=[Depends(authorize(SCOPE_QUEUES_ADMIN))],
)

# Short alias -> real ARQ queue (Redis sorted-set) name.
_QUEUES = {
    "external": EXTERNAL_QUEUE,
    "files": FILE_QUEUE,
}


def _paused_key(queue_name: str) -> str:
    return f"queue:paused:{queue_name}"


@router.get(
    "",
    response_model=DefaultResponse[list],
    responses=build_responses(include={status.HTTP_200_OK, status.HTTP_500_INTERNAL_SERVER_ERROR}),
)
async def list_queues(request: Request, response: Response):
    """List managed queues with their depth and paused flag."""
    try:
        arq = request.state.arq
        redis = request.state.redis
        data = []
        for alias, qname in sorted(_QUEUES.items()):
            depth = await arq.zcard(qname)
            paused = bool(await redis.exists(_paused_key(qname)))
            data.append({"queue": alias, "name": qname, "queued": int(depth), "paused": paused})
        return success_response(request=request, response=response, data=data)
    except Exception as ex:
        logger.error(f"list_queues failed: {ex}")
        return error_response(request=request, response=response, exc=ex)


def _resolve(queue: str):
    return _QUEUES.get(queue)


@router.post(
    "/{queue}/pause",
    responses=build_responses(include={status.HTTP_200_OK, status.HTTP_404_NOT_FOUND, status.HTTP_500_INTERNAL_SERVER_ERROR}),
)
async def pause_queue(queue: str, request: Request, response: Response):
    qname = _resolve(queue)
    if qname is None:
        return warning_response(request=request, response=response,
                                msg=f"Unknown queue '{queue}'. Known: {sorted(_QUEUES)}",
                                status_code=status.HTTP_404_NOT_FOUND)
    try:
        await request.state.redis.set(_paused_key(qname), "1")
        return success_response(request=request, response=response,
                                data={"queue": queue, "name": qname, "paused": True}, msg="Queue paused")
    except Exception as ex:
        logger.error(f"pause_queue failed: {ex}")
        return error_response(request=request, response=response, exc=ex)


@router.post(
    "/{queue}/resume",
    responses=build_responses(include={status.HTTP_200_OK, status.HTTP_404_NOT_FOUND, status.HTTP_500_INTERNAL_SERVER_ERROR}),
)
async def resume_queue(queue: str, request: Request, response: Response):
    qname = _resolve(queue)
    if qname is None:
        return warning_response(request=request, response=response,
                                msg=f"Unknown queue '{queue}'. Known: {sorted(_QUEUES)}",
                                status_code=status.HTTP_404_NOT_FOUND)
    try:
        await request.state.redis.delete(_paused_key(qname))
        return success_response(request=request, response=response,
                                data={"queue": queue, "name": qname, "paused": False}, msg="Queue resumed")
    except Exception as ex:
        logger.error(f"resume_queue failed: {ex}")
        return error_response(request=request, response=response, exc=ex)


@router.post(
    "/{queue}/purge",
    responses=build_responses(include={status.HTTP_200_OK, status.HTTP_404_NOT_FOUND, status.HTTP_500_INTERNAL_SERVER_ERROR}),
)
async def purge_queue(queue: str, request: Request, response: Response):
    """Remove all queued (not-yet-started) jobs from the queue."""
    qname = _resolve(queue)
    if qname is None:
        return warning_response(request=request, response=response,
                                msg=f"Unknown queue '{queue}'. Known: {sorted(_QUEUES)}",
                                status_code=status.HTTP_404_NOT_FOUND)
    try:
        arq = request.state.arq  # ArqRedis (binary) — operate on raw arq:* keys here, NOT the decoded cache client
        ids = await arq.zrange(qname, 0, -1)
        purged = 0
        if ids:
            keys = []
            for jid in ids:
                jid = jid.decode() if isinstance(jid, (bytes, bytearray)) else jid
                keys.append(f"arq:job:{jid}")
            if keys:
                await arq.delete(*keys)
            purged = len(ids)
        await arq.delete(qname)
        return success_response(request=request, response=response,
                                data={"queue": queue, "name": qname, "purged": purged}, msg="Queue purged")
    except Exception as ex:
        logger.error(f"purge_queue failed: {ex}")
        return error_response(request=request, response=response, exc=ex)
