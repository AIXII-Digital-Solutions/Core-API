"""
API-token authentication + scope authorization for the gateway.

Two credential types, BOTH accepted by ``authorize(*scopes)``:

  * ``X-Service-Token`` — the existing shared secret for FULLY-TRUSTED internal backends
    (the portal, the platform's own services). Grants every scope. Unchanged.
  * ``X-Api-Key: <prefix>.<secret>`` — a per-caller, DB-backed key (``api_tokens`` table) with
    an explicit scope set, optional expiry and a revocation (``enabled``) switch. This is how
    you give EXTERNAL people scoped access to this gateway.

How it fits the platform (what to tell API-key holders):
  An API key authorises the CALLER against THIS gateway ONLY. core-api still talks to the
  workers and file-processor with its OWN internal service tokens — an external key never
  reaches a worker, Redis, or a database directly. So granting someone ``flights:read`` lets
  them call core-api's flight endpoints; it does NOT expose the queues, the scheduler internals,
  or the workers. Scope what each person needs; revoke by disabling or deleting the row.

Only the sha256+pepper HASH of a secret is stored; the secret is shown once at creation.
"""
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Request, Header, HTTPException, status
from sqlalchemy import select

from settings import SERVICE_TOKEN, API_TOKEN_PEPPER
from Database import ApiToken

# --- Domain scopes -----------------------------------------------------------------------
# Keep these in sync with the docs and the /tokens admin router validation.
SCOPE_FLIGHTS_READ = "flights:read"
SCOPE_STATUS_READ = "status:read"
SCOPE_FILES_WRITE = "files:write"
SCOPE_SCHEDULER_READ = "scheduler:read"
SCOPE_SCHEDULER_WRITE = "scheduler:write"
SCOPE_QUEUES_ADMIN = "queues:admin"
SCOPE_TOKENS_ADMIN = "tokens:admin"
SCOPE_ADMIN = "admin"  # superscope: satisfies any required scope

ALL_SCOPES = {
    SCOPE_FLIGHTS_READ, SCOPE_STATUS_READ, SCOPE_FILES_WRITE,
    SCOPE_SCHEDULER_READ, SCOPE_SCHEDULER_WRITE, SCOPE_QUEUES_ADMIN,
    SCOPE_TOKENS_ADMIN, SCOPE_ADMIN,
}

# How long to coalesce last_used_at writes (avoid a DB write on every authorised request).
_LAST_USED_THROTTLE = timedelta(seconds=60)


def hash_secret(secret: str) -> str:
    """sha256 of pepper+secret. Constant for a given secret/pepper so it can be compared."""
    return hashlib.sha256(f"{API_TOKEN_PEPPER}{secret}".encode("utf-8")).hexdigest()


def _service_token_ok(x_service_token: Optional[str]) -> bool:
    # compare on bytes: header values may contain non-ASCII (latin-1), which would make
    # hmac.compare_digest raise on str inputs -> a 500 instead of a clean reject.
    return (bool(SERVICE_TOKEN) and bool(x_service_token)
            and hmac.compare_digest(x_service_token.encode("utf-8"), SERVICE_TOKEN.encode("utf-8")))


async def _lookup_api_token(request: Request, x_api_key: Optional[str]) -> Optional[ApiToken]:
    """Validate an X-Api-Key and return its ApiToken row, or None if invalid/expired/disabled."""
    if not x_api_key or "." not in x_api_key:
        return None
    prefix, _, secret = x_api_key.partition(".")
    if not prefix or not secret:
        return None
    now = datetime.now(timezone.utc)
    async with request.app.state.db_client.session("service") as session:
        row = (await session.execute(
            select(ApiToken).where(ApiToken.token_prefix == prefix)
        )).scalar_one_or_none()
        if row is None or not row.enabled:
            return None
        if not hmac.compare_digest(row.token_hash, hash_secret(secret)):
            return None
        if row.expires_at is not None and row.expires_at < now:
            return None
        # throttled best-effort last_used_at (coalesced so we don't write on every request)
        if row.last_used_at is None or (now - row.last_used_at) > _LAST_USED_THROTTLE:
            row.last_used_at = now
        return row  # detached after the session commits (expire_on_commit=False keeps attrs)


def authorize(*required_scopes: str):
    """FastAPI dependency factory. Allows the request if EITHER a valid service token is
    presented (internal, full access) OR a valid API key whose scopes cover ``required_scopes``
    (or holds the ``admin`` superscope). Returns the ApiToken (or None for the service token).
    """
    needed = set(required_scopes)

    async def dependency(
        request: Request,
        x_service_token: Optional[str] = Header(default=None),
        x_api_key: Optional[str] = Header(default=None),
    ) -> Optional[ApiToken]:
        if _service_token_ok(x_service_token):
            return None  # trusted internal caller — full access
        token = await _lookup_api_token(request, x_api_key)
        if token is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing or invalid credentials (X-Service-Token or X-Api-Key)",
            )
        have = set(token.scopes or [])
        if SCOPE_ADMIN not in have and not needed.issubset(have):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing required scope(s): {sorted(needed - have)}",
            )
        return token

    return dependency


__all__ = [
    "hash_secret", "authorize", "ALL_SCOPES",
    "SCOPE_FLIGHTS_READ", "SCOPE_STATUS_READ", "SCOPE_FILES_WRITE",
    "SCOPE_SCHEDULER_READ", "SCOPE_SCHEDULER_WRITE", "SCOPE_QUEUES_ADMIN",
    "SCOPE_TOKENS_ADMIN", "SCOPE_ADMIN",
]
