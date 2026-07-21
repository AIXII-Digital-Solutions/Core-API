"""
Power BI Embedded capacity control — portal-facing start/stop of the Azure capacity `a1azure`.

The portal calls these to pause/resume the capacity so it does not bill around the clock in a sandbox
with no production users. core-api holds the DEDICATED ARM service principal (PBIE_*) and talks to Azure
Resource Manager; the browser never reaches Azure. The ARM work lives in the framework-agnostic
``Utils.pbie_capacity.CapacityClient``, built once in the app lifespan and hung on ``app.state.capacity``.

Contract — all three return the standard DefaultResponse envelope; the portal reads ``.data``:

    GET  /capacity/state
    POST /capacity/pause     (X-Actor: <operator>)
    POST /capacity/resume    (X-Actor: <operator>)

    data = { "status": "live"|"paused"|"transitioning"|"failed", "embeddable": bool,
             "_raw_state": str, "_provisioning_state": str|null }

Status codes:
  200  success, including idempotent no-ops (pause an already-paused capacity issues no ARM POST)
  401  no credentials / 403  authenticated but missing the `capacity:admin` scope  — from authorize()
  409  an operation is already in flight — the portal should keep polling, not error
  502  ARM rejected the call or is unreachable (expired secret, missing role, bad resource, outage)
  503  capacity control is not configured on this server (PBIE_* unset / azure-identity missing)

The operation is ASYNCHRONOUS: the POST fires the ARM call (ARM answers 202 Accepted) and returns
immediately — the handler never blocks waiting for the capacity to finish transitioning. The portal
polls GET /capacity/state every ~10s until the status settles. WHICH portal users may control capacity
is decided by the portal (it authenticates the operator and passes them via X-Actor); this gateway only
requires the `capacity:admin` scope on the calling credential and records the actor in the audit log.
"""
from typing import Optional

from fastapi import Request, Response, Header, Depends, status

from settings import Router
from Config import setup_logger
from Database import ApiToken
from Utils import success_response, warning_response, error_response
from Utils.ResponsesFunc import build_responses
from Utils.pbie_capacity import CapacityState, CapacityBusy, CapacityError
from api_auth import authorize, SCOPE_CAPACITY_ADMIN

logger = setup_logger("capacity_api")

router = Router(prefix="/capacity", tags=["Capacity"])

_CODES = {
    status.HTTP_200_OK, status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN,
    status.HTTP_409_CONFLICT, status.HTTP_502_BAD_GATEWAY, status.HTTP_503_SERVICE_UNAVAILABLE,
}

_NOT_CONFIGURED = "Capacity control is not configured on this server (PBIE_* / azure-identity)."


def _serialise(state: CapacityState) -> dict:
    return {
        "status": state.status,
        "embeddable": state.embeddable,
        # Raw ARM values: useful in logs and support tickets, meaningless in end-user copy.
        "_raw_state": state.raw_state,
        "_provisioning_state": state.provisioning_state,
    }


def _actor(x_actor: Optional[str], token: Optional[ApiToken]) -> str:
    """Who to attribute a state-changing call to in the audit log. The portal authenticates the human
    operator and passes them via X-Actor; fall back to the API-key prefix or the trusted service token."""
    if x_actor:
        return x_actor
    if token is not None:
        return f"apikey:{token.token_prefix}"
    return "service-token"


def _client(request: Request):
    return getattr(request.app.state, "capacity", None)


@router.get("/state", responses=build_responses(include=_CODES))
async def read_state(
    request: Request, response: Response,
    _: Optional[ApiToken] = Depends(authorize(SCOPE_CAPACITY_ADMIN)),
):
    client = _client(request)
    if client is None:
        return error_response(request=request, response=response,
                              msg=_NOT_CONFIGURED, status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    try:
        return success_response(request=request, response=response, data=_serialise(await client.get_state()))
    except CapacityError as exc:
        logger.error("capacity ARM error op=state cid=%s: %s",
                     getattr(request.state, "correlation_id", None), exc)
        return error_response(request=request, response=response,
                              msg=f"Azure ARM error: {exc}", status_code=status.HTTP_502_BAD_GATEWAY)


@router.post("/pause", responses=build_responses(include=_CODES))
async def pause_capacity(
    request: Request, response: Response,
    x_actor: Optional[str] = Header(default=None, alias="X-Actor"),
    token: Optional[ApiToken] = Depends(authorize(SCOPE_CAPACITY_ADMIN)),
):
    return await _run("pause", request, response, _actor(x_actor, token))


@router.post("/resume", responses=build_responses(include=_CODES))
async def resume_capacity(
    request: Request, response: Response,
    x_actor: Optional[str] = Header(default=None, alias="X-Actor"),
    token: Optional[ApiToken] = Depends(authorize(SCOPE_CAPACITY_ADMIN)),
):
    return await _run("resume", request, response, _actor(x_actor, token))


async def _run(op: str, request: Request, response: Response, actor: str):
    client = _client(request)
    if client is None:
        return error_response(request=request, response=response,
                              msg=_NOT_CONFIGURED, status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    cid = getattr(request.state, "correlation_id", None)
    try:
        state = await (client.pause(actor) if op == "pause" else client.resume(actor))
        # Audit at the gateway: ties the ARM action to the correlation id, the actor and the outcome.
        # (This is a billing-affecting action on shared infrastructure.)
        logger.info("capacity.%s actor=%s cid=%s -> status=%s raw=%s",
                    op, actor, cid, state.status, state.raw_state)
        return success_response(request=request, response=response, data=_serialise(state))
    except CapacityBusy as exc:
        # 409, not an error toast: someone/something got there first — the portal just keeps polling.
        return warning_response(request=request, response=response,
                                msg=str(exc), status_code=status.HTTP_409_CONFLICT)
    except CapacityError as exc:
        logger.error("capacity ARM error op=%s actor=%s cid=%s: %s", op, actor, cid, exc)
        return error_response(request=request, response=response,
                              msg=f"Azure ARM error: {exc}", status_code=status.HTTP_502_BAD_GATEWAY)
