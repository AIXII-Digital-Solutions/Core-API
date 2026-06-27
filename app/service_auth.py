"""Server-to-server authentication via a shared service token.

Used by internal callers (e.g. the portal backend) that hit this gateway for
aviation data. Add ``Depends(verify_service_token)`` to any route that should be
reachable only by trusted backends.
"""
import hmac

from fastapi import Header, HTTPException, status

from settings import SERVICE_TOKEN


async def verify_service_token(x_service_token: str | None = Header(default=None)) -> None:
    """Reject the request unless a valid X-Service-Token header is presented.

    If SERVICE_TOKEN is unset the route is closed (denies all) rather than open.
    """
    if not SERVICE_TOKEN or not x_service_token or not hmac.compare_digest(x_service_token, SERVICE_TOKEN):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing service token",
        )


__all__ = ["verify_service_token"]
