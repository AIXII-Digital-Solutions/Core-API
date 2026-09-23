import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager

from arq import create_pool
from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from starlette.middleware.gzip import GZipMiddleware

import settings
from Config import setup_logger, DBSettings, secrets
from Database import DatabaseClient
from Queue import get_redis_settings
from Schemas import DefaultResponse, DetailField
from Utils import DBProxy
from Utils import RequestMetrics
from Utils.DomainCache import FLEET_WRITE_PREFIXES, invalidate_fleet

logger = setup_logger(
    'fastapi_app',
    log_format='%(levelname)s:     [%(name)s:%(process)d] %(asctime)s | %(message)s'
)


@asynccontextmanager
async def lifespan(app):
    """
    API Server lifespan. Unlike the old monolith, this process is ONLY the HTTP API:
    no scheduler, no file-processing loops. Background work is delegated to the
    file_processor and external_worker services via the ARQ (Redis) broker.
    """
    username, password, host, port = DBSettings().get_reddis_credentials()
    logger.info("Startup initiated...")
    app.state.redis = Redis(username=username or None, password=password or None, host=host, port=port, decode_responses=True)
    app.state.db_client = DatabaseClient()
    app.state.db_proxy = DBProxy(app.state.redis)
    # ARQ pool for enqueuing jobs to the worker segments
    app.state.arq = await create_pool(get_redis_settings())
    logger.info("Redis, DatabaseClient and ARQ pool initialized")

    # Power BI Embedded capacity control (optional). Built ONCE here (azure-identity caches/refreshes
    # the ARM token). If PBIE_* is unconfigured or azure-identity is not installed, leave it None — the
    # /capacity endpoints then return 503 and the rest of the API boots normally.
    app.state.capacity = None
    _pbie = (settings.PBIE_TENANT_ID, settings.PBIE_CLIENT_ID, settings.PBIE_CLIENT_SECRET,
             settings.PBIE_SUBSCRIPTION_ID, settings.PBIE_RESOURCE_GROUP, settings.PBIE_CAPACITY_NAME)
    if all(_pbie):
        try:
            from Utils.pbie_capacity import CapacityClient
            app.state.capacity = CapacityClient(
                tenant_id=settings.PBIE_TENANT_ID, client_id=settings.PBIE_CLIENT_ID,
                client_secret=settings.PBIE_CLIENT_SECRET, subscription_id=settings.PBIE_SUBSCRIPTION_ID,
                resource_group=settings.PBIE_RESOURCE_GROUP, capacity_name=settings.PBIE_CAPACITY_NAME,
            )
            logger.info("PBIE CapacityClient initialized (capacity=%s)", settings.PBIE_CAPACITY_NAME)
        except Exception as ex:
            logger.warning("PBIE CapacityClient NOT initialized (capacity endpoints will 503): %s", ex)
    else:
        logger.info("PBIE_* not fully set — capacity control disabled (endpoints will 503)")

    logger.info("Startup completed. Welcome :O")

    yield

    logger.info("Shutdown initiated...")
    if getattr(app.state, "capacity", None) is not None:
        try:
            await app.state.capacity.aclose()
        except Exception:
            pass
    try:
        await app.state.arq.aclose()
    except Exception:
        pass
    logger.info("Closing redis connection...")
    await app.state.redis.aclose()
    logger.info("Closing database connection...")
    await app.state.db_client.dispose()
    # Lock the vault. No-op under SECRETS_BACKEND=env; under `vaultwarden` it drops the cached
    # values and the session key so a lingering process cannot be used to read the vault.
    try:
        await asyncio.to_thread(secrets.close_provider)
    except Exception:
        pass
    logger.info("Shutdown completed. Bye!")


class RequestContextMiddleware:
    """Per-request context, timing and logging — ONE pure-ASGI layer.

    This replaces two `@app.middleware("http")` functions. Starlette turns each of those into a
    BaseHTTPMiddleware, which re-wraps the response in its own anyio streams and task group on every
    request: measurable overhead per layer, and it gets in the way of streaming (the SSE status feed).
    A plain ASGI callable does the same job by touching only the scope and the response-start message.

    What it sets up, unchanged for the code downstream: `request.state.correlation_id`, `.redis`, `.arq`
    and `.db_proxy` (Starlette's `request.state` IS `scope["state"]`), the `X-Correlation-ID` response
    header, one log line per request, and closing any sessions DBProxy opened.

    It also adds `Server-Timing: app;dur=<ms>` — the time spent inside this process up to the response
    headers. The API sits behind a proxy on another machine, so a client's latency mixes network, proxy
    and application; this header is how to tell them apart without a login to the server."""

    def __init__(self, app, fastapi_app):
        self.app = app
        self.fastapi_app = fastapi_app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        cost = RequestMetrics.begin()
        correlation_id = str(uuid.uuid4())
        app_state = self.fastapi_app.state
        db_proxy = DBProxy(app_state.redis)
        state = scope.setdefault("state", {})
        state["correlation_id"] = correlation_id
        state["redis"] = app_state.redis
        state["arq"] = app_state.arq
        state["db_proxy"] = db_proxy
        status_code = 500   # what gets logged if the app raises before sending anything
        cid_header = correlation_id.encode("ascii")

        async def send_with_context(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                elapsed_ms = (time.perf_counter() - started) * 1000
                message["headers"] = [
                    *message.get("headers", []),
                    (b"x-correlation-id", cid_header),
                    (b"server-timing", f"app;dur={elapsed_ms:.1f}".encode("ascii")),
                ]
            await send(message)

        try:
            await self.app(scope, receive, send_with_context)
        finally:
            elapsed = time.perf_counter() - started
            # The query string belongs in the line: `/fleet/aircraft` and
            # `/fleet/aircraft?limit=500&q=...` are different requests with very different costs,
            # and without it they are the same line twice.
            query = scope.get("query_string") or b""
            path = scope.get("path", "")
            if query:
                path = f"{path}?{query.decode('latin-1')[:200]}"

            # Three levels, decided by what is wrong rather than by what happened: a failure, a
            # request that took too long, or one that asked the database too many times. The last
            # is the one that matters most and shows up least - it is a shape that is merely slow
            # here and much worse over a longer wire.
            if status_code >= 500:
                level = logging.ERROR
            elif elapsed * 1000 >= settings.SLOW_REQUEST_MS or status_code >= 400:
                level = logging.WARNING
            elif cost.queries >= settings.BUSY_REQUEST_QUERIES:
                level = logging.WARNING
            else:
                level = logging.INFO

            # Lazy %-formatting: with logging going through a queue, the message is only built by
            # the listener thread, never on the event loop.
            logger.log(level, "%s %s completed_in=%.3fs | status_code=%s | %s | correlation_id=%s",
                       scope.get("method"), path, elapsed, status_code, cost.summary(),
                       correlation_id)
            # Name the query only when the request was worth complaining about. Logging every
            # statement of every request is how a log becomes something nobody reads.
            # One place, so a new write handler cannot forget it. A 2xx to a non-GET under a
            # domain prefix means something changed; anything else means nothing did.
            if (scope.get("method") not in ("GET", "HEAD", "OPTIONS")
                    and status_code < 400
                    and any(f"{p}/" in scope.get("path", "") or scope.get("path", "").endswith(p)
                            for p in FLEET_WRITE_PREFIXES)):
                await invalidate_fleet(getattr(app_state, "redis", None))

            if level >= logging.WARNING and cost.slowest_statement:
                logger.log(level, "  slowest statement of %s: %.1fms  %s",
                           correlation_id, cost.slowest_seconds * 1000, cost.slowest_statement)
            await db_proxy.close_all()


def register_middlewares(app):
    # Order matters: add_middleware wraps from the inside out, so the LAST one added is the outermost.
    # Context first (innermost of the two) so its Server-Timing header is in place before GZip, the
    # outer layer, compresses the body.
    app.add_middleware(RequestContextMiddleware, fastapi_app=app)
    # JSON compresses 5-10x, and the heavy payloads here are JSON — including the FileResponse downloads
    # under /database and /flightradar. Over a link with a ~150 ms round trip every TCP window a response
    # needs costs a round trip, so shrinking a payload shortens the wall time, not just the bill.
    # compresslevel 5, not the default 9: most of the ratio for a fraction of the CPU on the API host.
    # Server-sent events are excluded by Starlette itself (text/event-stream is never buffered).
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)

    # Custom ValidationError handler
    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        correlation_id = getattr(request.state, "correlation_id", None)

        details = [
            {
                "field": ".".join(map(str, e["loc"])),
                "msg": e["msg"],
                "correlationId": correlation_id,
            }
            for e in exc.errors()
        ]

        response = DefaultResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            details=DetailField(
                msg="Validation error",
                correlationId=correlation_id
            ),
            data=details
        )

        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=response.model_dump(mode="json")
        )

    # Custom 500 exception handler
    @app.exception_handler(Exception)
    async def custom_exception_handler(request: Request, exc: Exception):
        correlation_id = getattr(request.state, "correlation_id", None)
        # Full detail goes to the logs only; the client gets a generic message + the
        # correlation id (so ops can find the matching log) — never the exception internals.
        logger.critical(f"Unhandled error: {exc} \n CorrelationID = {correlation_id}", exc_info=True)
        response = DefaultResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            details=DetailField(
                msg="Internal server error",
                correlationId=correlation_id
            ),
            data=None
        )

        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=response.model_dump(mode="json")
        )


__all__ = ["register_middlewares", "lifespan"]
