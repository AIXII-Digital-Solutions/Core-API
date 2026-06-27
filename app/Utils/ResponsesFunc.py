from typing import Optional, TypeVar, Set
from http import HTTPStatus


from fastapi import Request, status, Response

from Config import setup_logger
from Schemas import DetailField, DefaultResponse, ErrorResponse

logger = setup_logger("responses")

T = TypeVar("T")

def build_responses(*, include: Set[int]) -> dict:
    result = {}

    success_codes = {200, 201, 202}

    for status_code in include:
        if status_code in success_codes:
            continue

        result[status_code] = {
            "description": HTTPStatus(status_code).phrase,
            "model": ErrorResponse
        }

    return result


def success_response(*, request: Request, response: Response, data: T, msg: str = "Success",
                     status_code: status = status.HTTP_200_OK) -> DefaultResponse[T]:
    response.status_code = status_code
    return DefaultResponse(
        status_code=status_code,
        details=DetailField(
            msg=msg,
            correlationId=request.state.correlation_id
        ),
        data=data
    )


def _safe_msg(*, request: Request, exc: Optional[Exception], msg: Optional[str],
              status_code: int, generic: str) -> str:
    """Never leak exception internals to the client. When only an exception is given, it is
    LOGGED server-side (with the correlation id) and a generic message is returned; a caller
    -supplied ``msg`` is always a controlled string and is used as-is."""
    correlation_id = getattr(request.state, "correlation_id", None)
    if msg:
        return msg
    # exc-only path: log full detail, return a safe generic message
    logger.error("response %s cid=%s: %s: %s", status_code, correlation_id,
                 exc.__class__.__name__, exc, exc_info=True)
    return generic


def warning_response(*, request: Request, response: Response,
                     exc: Optional[Exception] = None,
                     msg: Optional[str] = None,
                     status_code: status = status.HTTP_400_BAD_REQUEST) -> DefaultResponse[T]:
    if not exc and not msg:
        raise ValueError("'exc' or 'msg' must be provided")
    response.status_code = status_code
    client_msg = _safe_msg(request=request, exc=exc, msg=msg, status_code=status_code, generic="Request error")
    return DefaultResponse(
        status_code=status_code,
        details=DetailField(msg=client_msg, correlationId=getattr(request.state, "correlation_id", None)),
        data=[]
    )


def error_response(*, request: Request, response: Response,
                   exc: Optional[Exception] = None,
                   msg: Optional[str] = None,
                   status_code: status = status.HTTP_500_INTERNAL_SERVER_ERROR) -> DefaultResponse[T]:
    if not exc and not msg:
        raise ValueError("'exc' or 'msg' must be provided")
    response.status_code = status_code
    client_msg = _safe_msg(request=request, exc=exc, msg=msg, status_code=status_code, generic="Internal server error")
    return DefaultResponse(
        status_code=status_code,
        details=DetailField(msg=client_msg, correlationId=getattr(request.state, "correlation_id", None)),
        data=[]
    )
