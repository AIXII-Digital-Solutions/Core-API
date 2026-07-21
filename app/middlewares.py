import asyncio
import uuid
from contextlib import asynccontextmanager

from arq import create_pool
from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

import settings
from Config import setup_logger, DBSettings
from Database import DatabaseClient
from Queue import get_redis_settings
from Schemas import DefaultResponse, DetailField
from Utils import DBProxy

logger = setup_logger(
    'fastapi_app',
    log_format='%(levelname)s:     [%(name)s] %(asctime)s | %(message)s'
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
    logger.info("Shutdown completed. Bye!")


def register_middlewares(app):
    # Middleware for requests + logging and db/cache
    @app.middleware("http")
    async def log_and_db_requests(request: Request, call_next):
        start_time = asyncio.get_event_loop().time()
        request.state.redis = app.state.redis
        request.state.db_proxy = DBProxy(app.state.redis)
        request.state.arq = app.state.arq

        try:
            response = await call_next(request)

            duration = asyncio.get_event_loop().time() - start_time
            logger.info(
                f"{request.method} {request.url.path} completed_in={duration:.2f}s | "
                f"status_code={response.status_code} | "
                f"correlation_id={getattr(request.state, 'correlation_id', None)}"
            )
            return response
        finally:
            await request.state.db_proxy.close_all()

    # Middleware for add correlation id to requests
    @app.middleware("http")
    async def add_correlation_id(request: Request, call_next):
        correlation_id = str(uuid.uuid4())
        request.state.correlation_id = correlation_id
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = correlation_id
        return response

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
