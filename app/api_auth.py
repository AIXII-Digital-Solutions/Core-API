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

Both credentials are declared as OpenAPI SECURITY SCHEMES rather than plain header parameters. That
difference is invisible at runtime and decisive in the docs: a scheme puts them behind Swagger's
Authorize button — entered once, sent with every request — while a header parameter is a box to
retype on every single operation, which is how a working credential comes to look like a rejected
one. The schemes are listed side by side (OpenAPI reads a LIST of requirements as OR), which is
exactly what authorize() does: either credential is enough.
"""
import hashlib
import hmac
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, Request, HTTPException, status
from fastapi.security import APIKeyHeader
from sqlalchemy import select, update

from Config import setup_logger
from settings import SERVICE_TOKEN, API_TOKEN_PEPPER
from Database import ApiToken

logger = setup_logger("api_auth")

# --- Domain scopes -----------------------------------------------------------------------
# Keep these in sync with the docs and the /tokens admin router validation.
SCOPE_FLIGHTS_READ = "flights:read"
SCOPE_STATUS_READ = "status:read"
SCOPE_FILES_WRITE = "files:write"
SCOPE_SCHEDULER_READ = "scheduler:read"
SCOPE_SCHEDULER_WRITE = "scheduler:write"
SCOPE_QUEUES_ADMIN = "queues:admin"
SCOPE_TOKENS_ADMIN = "tokens:admin"
SCOPE_PREDICTIVE_READ = "predictive:read"
SCOPE_PREDICTIVE_WRITE = "predictive:write"
SCOPE_CAPACITY_ADMIN = "capacity:admin"  # start/stop the Power BI Embedded Azure capacity
SCOPE_INSURANCE_READ = "insurance:read"
SCOPE_INSURANCE_WRITE = "insurance:write"
SCOPE_ADMIN = "admin"  # superscope: satisfies any required scope

ALL_SCOPES = {
    SCOPE_FLIGHTS_READ, SCOPE_STATUS_READ, SCOPE_FILES_WRITE,
    SCOPE_SCHEDULER_READ, SCOPE_SCHEDULER_WRITE, SCOPE_QUEUES_ADMIN,
    SCOPE_TOKENS_ADMIN, SCOPE_PREDICTIVE_READ, SCOPE_PREDICTIVE_WRITE,
    SCOPE_CAPACITY_ADMIN, SCOPE_INSURANCE_READ, SCOPE_INSURANCE_WRITE, SCOPE_ADMIN,
}

# How long to coalesce last_used_at writes (avoid a DB write on every authorised request).
_LAST_USED_THROTTLE = timedelta(seconds=60)


# auto_error=False on both: a missing credential must fall through to OUR check, which answers 401
# naming BOTH headers. Left at the default, FastAPI would reject first with a bare 403 that tells the
# caller nothing about which credential it wanted.
SERVICE_TOKEN_HEADER = APIKeyHeader(
    name="X-Service-Token", scheme_name="ServiceToken", auto_error=False,
    description="The master service token. Full access, no scopes — for the platform's own "
                "backends. Either this or X-Api-Key.")
API_KEY_HEADER = APIKeyHeader(
    name="X-Api-Key", scheme_name="ApiKey", auto_error=False,
    description="A scoped key, `<prefix>.<secret>`, minted at /tokens. Its scopes must cover the "
                "endpoint (or hold `admin`). Either this or X-Service-Token.")


def hash_secret(secret: str) -> str:
    """sha256 of pepper+secret. Constant for a given secret/pepper so it can be compared."""
    return hashlib.sha256(f"{API_TOKEN_PEPPER}{secret}".encode("utf-8")).hexdigest()


def _service_token_ok(x_service_token: Optional[str]) -> bool:
    # compare on bytes: header values may contain non-ASCII (latin-1), which would make
    # hmac.compare_digest raise on str inputs -> a 500 instead of a clean reject.
    return (bool(SERVICE_TOKEN) and bool(x_service_token)
            and hmac.compare_digest(x_service_token.encode("utf-8"), SERVICE_TOKEN.encode("utf-8")))


# Validated keys are remembered IN THIS PROCESS for a few seconds. Checking a key otherwise costs a
# transaction on the service database for every request (BEGIN, SELECT, COMMIT — three round trips to
# a database on another machine), which is more than most endpoints spend on their own work.
# The price is the window: disabling, deleting or narrowing a key takes up to this long to reach every
# API worker (the one that served the /tokens change drops its entry at once). 0 turns the cache off.
_TOKEN_CACHE_TTL_S = float(os.getenv("API_TOKEN_CACHE_SECONDS", "10"))
_TOKEN_CACHE_MAX = 1024
# sha256(full header value) -> (cached_at monotonic, detached ApiToken). Never the raw secret as a key.
_token_cache: dict[str, tuple[float, ApiToken]] = {}


def forget_cached_tokens(token_prefix: Optional[str] = None) -> None:
    """Drop cached keys — one prefix, or all. Called by the /tokens router after it changes a key."""
    if token_prefix is None:
        _token_cache.clear()
        return
    for k in [k for k, (_, row) in _token_cache.items() if row.token_prefix == token_prefix]:
        _token_cache.pop(k, None)


async def _load_api_token(request: Request, prefix: str, secret: str) -> Optional[ApiToken]:
    """The database check: the row behind ``prefix``, if it exists, is enabled and the secret matches."""
    async with request.app.state.db_client.read_session("service") as session:
        row = (await session.execute(
            select(ApiToken).where(ApiToken.token_prefix == prefix)
        )).scalar_one_or_none()
        if row is None or not row.enabled:
            return None
        if not hmac.compare_digest(row.token_hash, hash_secret(secret)):
            return None
        session.expunge(row)      # detached: it outlives the session and is shared by cached hits
        return row


async def _lookup_api_token(request: Request, x_api_key: Optional[str]) -> Optional[ApiToken]:
    """Validate an X-Api-Key and return its ApiToken row, or None if invalid/expired/disabled."""
    if not x_api_key or "." not in x_api_key:
        return None
    prefix, _, secret = x_api_key.partition(".")
    if not prefix or not secret:
        return None
    now = datetime.now(timezone.utc)
    key = hashlib.sha256(x_api_key.encode("utf-8")).hexdigest()
    hit = _token_cache.get(key)
    if hit is not None and time.monotonic() - hit[0] < _TOKEN_CACHE_TTL_S:
        row = hit[1]
    else:
        # Only successes are cached: a wrong key always goes to the database, so the cache can never
        # turn a rejected key into an accepted one.
        row = await _load_api_token(request, prefix, secret)
        if row is None:
            _token_cache.pop(key, None)
            return None
        if _TOKEN_CACHE_TTL_S > 0:
            if len(_token_cache) >= _TOKEN_CACHE_MAX:
                _token_cache.clear()
            _token_cache[key] = (time.monotonic(), row)
    if row.expires_at is not None and row.expires_at < now:
        return None
    # Throttled best-effort last_used_at, coalesced so it is not written on every request.
    #
    # It goes through a TRANSACTIONAL session even though it is one statement: `read_session` is
    # documented as "never write through it", and an exception to that rule sitting in the auth
    # path is the one most likely to be copied. Two extra round trips, once per key per throttle
    # window, is not a price worth arguing about.
    #
    # And it cannot fail the request. This is bookkeeping: a database hiccup here must not turn a
    # valid credential into a 500 for the caller holding it.
    if row.last_used_at is None or (now - row.last_used_at) > _LAST_USED_THROTTLE:
        row.last_used_at = now
        try:
            async with request.app.state.db_client.session("service") as session:
                await session.execute(
                    update(ApiToken).where(ApiToken.id == row.id).values(last_used_at=now))
        except Exception:
            logger.warning("could not record last_used_at for token %s", row.prefix, exc_info=True)
    return row


def authorize(*required_scopes: str):
    """FastAPI dependency factory. Allows the request if EITHER a valid service token is
    presented (internal, full access) OR a valid API key whose scopes cover ``required_scopes``
    (or holds the ``admin`` superscope). Returns the ApiToken (or None for the service token).
    """
    needed = set(required_scopes)

    async def dependency(
        request: Request,
        x_service_token: Optional[str] = Depends(SERVICE_TOKEN_HEADER),
        x_api_key: Optional[str] = Depends(API_KEY_HEADER),
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
    "hash_secret", "authorize", "forget_cached_tokens", "ALL_SCOPES",
    "SERVICE_TOKEN_HEADER", "API_KEY_HEADER",
    "SCOPE_FLIGHTS_READ", "SCOPE_STATUS_READ", "SCOPE_FILES_WRITE",
    "SCOPE_SCHEDULER_READ", "SCOPE_SCHEDULER_WRITE", "SCOPE_QUEUES_ADMIN",
    "SCOPE_TOKENS_ADMIN", "SCOPE_PREDICTIVE_READ", "SCOPE_PREDICTIVE_WRITE",
    "SCOPE_CAPACITY_ADMIN", "SCOPE_INSURANCE_READ", "SCOPE_INSURANCE_WRITE", "SCOPE_ADMIN",
]
