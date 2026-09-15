import datetime
import decimal
import ipaddress
import pathlib
from typing import Optional, TypeVar, Set, Union
from http import HTTPStatus

import orjson
from fastapi import Request, status, Response
from pydantic import BaseModel

from Config import setup_logger
from Schemas import DetailField, DefaultResponse, ErrorResponse

logger = setup_logger("responses")

T = TypeVar("T")

_ORJSON_OPTIONS = orjson.OPT_NON_STR_KEYS


def _orjson_default(obj):
    """The types orjson does not handle natively, encoded the way fastapi's jsonable_encoder encodes them,
    so a client sees the same values as before. Anything else raises and the caller falls back."""
    if isinstance(obj, decimal.Decimal):
        # jsonable_encoder's decimal_encoder: an integral Decimal becomes an int, any other a float
        return int(obj) if obj.as_tuple().exponent >= 0 else float(obj)
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, datetime.timedelta):
        return obj.total_seconds()
    if isinstance(obj, (set, frozenset)):
        return list(obj)
    if isinstance(obj, (pathlib.PurePath, ipaddress._BaseAddress, ipaddress._BaseNetwork)):
        return str(obj)
    if isinstance(obj, bytes):
        return obj.decode()
    raise TypeError(f"not natively encodable: {type(obj).__name__}")


def _render(request: Request, payload: DefaultResponse, status_code: int) -> Union[Response, DefaultResponse]:
    """Serialize the envelope straight to bytes when that is safe, instead of handing the model back.

    Returned as a model, FastAPI runs it through jsonable_encoder — a recursive pure-Python walk of every
    value — and then json.dumps. orjson (C) does the same job several times faster, which matters for
    the list endpoints and is free for the rest.

    The fast path is skipped for a route that declares a `response_model`: returning a ready Response
    bypasses FastAPI's response validation, and a route may be relying on its model to FILTER fields
    out of the payload. It also steps aside for any value orjson cannot encode the way jsonable_encoder
    would — both cases get the old path, unchanged."""
    route = request.scope.get("route")
    if route is not None and getattr(route, "response_model", None) is not None:
        return payload
    try:
        body = orjson.dumps(payload.model_dump(), default=_orjson_default, option=_ORJSON_OPTIONS)
    except (TypeError, ValueError):
        return payload
    return Response(content=body, status_code=status_code, media_type="application/json")

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
                     status_code: status = status.HTTP_200_OK) -> Union[Response, DefaultResponse[T]]:
    response.status_code = status_code
    return _render(request, DefaultResponse(
        status_code=status_code,
        details=DetailField(
            msg=msg,
            correlationId=request.state.correlation_id
        ),
        data=data
    ), status_code)


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
    return _render(request, DefaultResponse(
        status_code=status_code,
        details=DetailField(msg=client_msg, correlationId=getattr(request.state, "correlation_id", None)),
        data=[]
    ), status_code)


def error_response(*, request: Request, response: Response,
                   exc: Optional[Exception] = None,
                   msg: Optional[str] = None,
                   status_code: status = status.HTTP_500_INTERNAL_SERVER_ERROR) -> DefaultResponse[T]:
    if not exc and not msg:
        raise ValueError("'exc' or 'msg' must be provided")
    response.status_code = status_code
    client_msg = _safe_msg(request=request, exc=exc, msg=msg, status_code=status_code, generic="Internal server error")
    return _render(request, DefaultResponse(
        status_code=status_code,
        details=DetailField(msg=client_msg, correlationId=getattr(request.state, "correlation_id", None)),
        data=[]
    ), status_code)
