"""
API-token administration (issue / list / update / revoke gateway API keys).

Creates rows in ``api_tokens`` and returns the full key ``<prefix>.<secret>`` ONCE at
creation (only its hash is stored). Protected by the ``tokens:admin`` scope — which the
master ``X-Service-Token`` always satisfies — so the owner can mint keys for other people.

See app/api_auth.py for how the issued keys are then used (X-Api-Key + scopes) and the
boundary they grant (this gateway only, never the workers directly).
"""
import secrets
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import Request, Response, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from settings import Router
from Config import setup_logger
from Database import ApiToken
from Utils import success_response, warning_response, error_response
from Utils.ResponsesFunc import build_responses
from api_auth import authorize, hash_secret, ALL_SCOPES, SCOPE_TOKENS_ADMIN, SCOPE_ADMIN

logger = setup_logger("tokens_api")

router = Router(prefix="/tokens", tags=["API Tokens"])


class TokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    scopes: List[str] = Field(default_factory=list)
    expires_at: Optional[datetime] = None  # optional absolute expiry (ISO 8601)


class TokenPatch(BaseModel):
    name: Optional[str] = Field(default=None, max_length=128)
    enabled: Optional[bool] = None
    scopes: Optional[List[str]] = None
    expires_at: Optional[datetime] = None


def _serialize(t: ApiToken) -> dict:
    """Public representation — NEVER includes the secret or its hash."""
    return {
        "prefix": t.token_prefix,
        "name": t.name,
        "scopes": t.scopes or [],
        "enabled": t.enabled,
        "expires_at": t.expires_at.isoformat() if t.expires_at else None,
        "last_used_at": t.last_used_at.isoformat() if t.last_used_at else None,
        "created_by": t.created_by,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


def _bad_scopes(scopes: List[str]) -> List[str]:
    return sorted(set(scopes) - ALL_SCOPES)


def _escalation(creator: Optional[ApiToken], scopes: List[str]) -> List[str]:
    """Scopes the creator may NOT grant (least privilege). The master service token (creator
    is None) and an ``admin`` key may grant anything; any other key may only grant scopes it
    itself holds — so a ``tokens:admin`` holder cannot self-escalate to ``admin``."""
    if creator is None:
        return []
    have = set(creator.scopes or [])
    if SCOPE_ADMIN in have:
        return []
    return sorted(set(scopes) - have)


def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


@router.post(
    "",
    responses=build_responses(include={status.HTTP_201_CREATED, status.HTTP_400_BAD_REQUEST, status.HTTP_500_INTERNAL_SERVER_ERROR}),
)
async def create_token(
    body: TokenCreate,
    request: Request,
    response: Response,
    creator: Optional[ApiToken] = Depends(authorize(SCOPE_TOKENS_ADMIN)),
):
    """Issue a new API key. The full key is returned ONCE — store it now, it can't be re-shown."""
    bad = _bad_scopes(body.scopes)
    if bad:
        return warning_response(request=request, response=response,
                                msg=f"Unknown scope(s): {bad}. Allowed: {sorted(ALL_SCOPES)}",
                                status_code=status.HTTP_400_BAD_REQUEST)
    denied = _escalation(creator, body.scopes)
    if denied:
        return warning_response(request=request, response=response,
                                msg=f"Cannot grant scope(s) you don't hold: {denied}",
                                status_code=status.HTTP_403_FORBIDDEN)
    try:
        prefix = "ak_" + secrets.token_hex(8)
        secret = secrets.token_urlsafe(32)
        row = ApiToken(
            name=body.name,
            token_prefix=prefix,
            token_hash=hash_secret(secret),
            scopes=body.scopes,
            enabled=True,
            expires_at=_as_utc(body.expires_at),
            created_by=(creator.name if creator else "service-token"),
        )
        async with request.app.state.db_client.session("service") as session:
            session.add(row)
            await session.flush()
            data = _serialize(row)
        data["api_key"] = f"{prefix}.{secret}"  # shown ONCE
        logger.info("api token issued: prefix=%s name=%s scopes=%s", prefix, body.name, body.scopes)
        return success_response(request=request, response=response, data=data,
                                msg="API key created — copy it now, it will not be shown again",
                                status_code=status.HTTP_201_CREATED)
    except Exception as ex:
        logger.error(f"create_token failed: {ex}")
        return error_response(request=request, response=response, exc=ex)


@router.get(
    "",
    responses=build_responses(include={status.HTTP_200_OK, status.HTTP_500_INTERNAL_SERVER_ERROR}),
)
async def list_tokens(
    request: Request,
    response: Response,
    _auth: Optional[ApiToken] = Depends(authorize(SCOPE_TOKENS_ADMIN)),
):
    try:
        async with request.app.state.db_client.session("service") as session:
            rows = (await session.execute(select(ApiToken).order_by(ApiToken.created_at.desc()))).scalars().all()
        return success_response(request=request, response=response, data=[_serialize(r) for r in rows])
    except Exception as ex:
        logger.error(f"list_tokens failed: {ex}")
        return error_response(request=request, response=response, exc=ex)


@router.patch(
    "/{prefix}",
    responses=build_responses(include={status.HTTP_200_OK, status.HTTP_400_BAD_REQUEST, status.HTTP_404_NOT_FOUND, status.HTTP_500_INTERNAL_SERVER_ERROR}),
)
async def update_token(
    prefix: str,
    body: TokenPatch,
    request: Request,
    response: Response,
    creator: Optional[ApiToken] = Depends(authorize(SCOPE_TOKENS_ADMIN)),
):
    """Enable/disable, rename, re-scope or set/clear the expiry of an existing key.

    Send ``expires_at: null`` explicitly to clear an expiry (make the key non-expiring);
    omitting the field leaves it unchanged.
    """
    if body.scopes is not None:
        bad = _bad_scopes(body.scopes)
        if bad:
            return warning_response(request=request, response=response,
                                    msg=f"Unknown scope(s): {bad}. Allowed: {sorted(ALL_SCOPES)}",
                                    status_code=status.HTTP_400_BAD_REQUEST)
        denied = _escalation(creator, body.scopes)
        if denied:
            return warning_response(request=request, response=response,
                                    msg=f"Cannot grant scope(s) you don't hold: {denied}",
                                    status_code=status.HTTP_403_FORBIDDEN)
    try:
        async with request.app.state.db_client.session("service") as session:
            row = (await session.execute(select(ApiToken).where(ApiToken.token_prefix == prefix))).scalar_one_or_none()
            if row is None:
                return warning_response(request=request, response=response,
                                        msg=f"No token with prefix '{prefix}'", status_code=status.HTTP_404_NOT_FOUND)
            if body.name is not None:
                row.name = body.name
            if body.enabled is not None:
                row.enabled = body.enabled
            if body.scopes is not None:
                row.scopes = body.scopes
            if "expires_at" in body.model_fields_set:   # explicit null clears the expiry
                row.expires_at = _as_utc(body.expires_at)
            data = _serialize(row)
        return success_response(request=request, response=response, data=data, msg="Token updated")
    except Exception as ex:
        logger.error(f"update_token failed: {ex}")
        return error_response(request=request, response=response, exc=ex)


@router.delete(
    "/{prefix}",
    responses=build_responses(include={status.HTTP_200_OK, status.HTTP_404_NOT_FOUND, status.HTTP_500_INTERNAL_SERVER_ERROR}),
)
async def revoke_token(
    prefix: str,
    request: Request,
    response: Response,
    _auth: Optional[ApiToken] = Depends(authorize(SCOPE_TOKENS_ADMIN)),
):
    """Permanently delete (revoke) a key."""
    try:
        async with request.app.state.db_client.session("service") as session:
            row = (await session.execute(select(ApiToken).where(ApiToken.token_prefix == prefix))).scalar_one_or_none()
            if row is None:
                return warning_response(request=request, response=response,
                                        msg=f"No token with prefix '{prefix}'", status_code=status.HTTP_404_NOT_FOUND)
            await session.delete(row)
        return success_response(request=request, response=response, data={"prefix": prefix}, msg="Token revoked")
    except Exception as ex:
        logger.error(f"revoke_token failed: {ex}")
        return error_response(request=request, response=response, exc=ex)
