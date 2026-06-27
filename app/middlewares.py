import asyncio
import uuid
from contextlib import asynccontextmanager

from arq import create_pool
from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

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
    logger.info("Startup completed. Welcome :O")

    yield

    logger.info("Shutdown initiated...")
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
        logger.critical(f"Unhandled error: {exc} \n CorrelationID = {correlation_id}", exc_info=True)
        response = DefaultResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            details=DetailField(
                msg=f"{exc.__class__.__name__}: {str(exc)}",
                correlationId=correlation_id
            ),
            data=None
        )

        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=response.model_dump(mode="json")
        )


__all__ = ["register_middlewares", "lifespan"]
