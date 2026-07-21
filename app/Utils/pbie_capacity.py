"""
pbie_capacity.py — async Azure Resource Manager (ARM) client for start/stop control of the
Power BI Embedded capacity ``a1azure`` (Microsoft.PowerBIDedicated, api-version 2021-01-01).

Framework-agnostic ON PURPOSE: no FastAPI/web imports, no ``settings``/``Config`` import, and no
module-level environment reads. The web layer (``Routers/Capacity.py``) builds ONE ``CapacityClient``
from ``settings.PBIE_*`` in the app lifespan and hangs it on ``app.state``; this module only knows
ARM and the state model. That keeps it unit-checkable (see ``_selftest`` at the bottom — pure
``classify`` with no network and no azure-identity) and free of any credential coupling.

Async throughout (httpx + ``azure.identity.aio``) because core-api is an async FastAPI app: a blocking
sync call inside a request handler would stall the event loop. azure-identity caches and refreshes the
ARM token in-process, so the credential is built ONCE per client and reused.

The ARM token audience is ``https://management.azure.com/.default`` — NOT the Power BI API audience
(``https://analysis.windows.net/powerbi/api/.default``) used for embed tokens. Different scope, separate
token, and Power BI API permissions confer nothing on ARM. The control identity is a DEDICATED service
principal (PBIE_CLIENT_ID) with a narrow custom role (read/suspend/resume only) — never the embed SPN.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal, Optional

log = logging.getLogger(__name__)

ARM = "https://management.azure.com"
ARM_SCOPE = "https://management.azure.com/.default"
API_VERSION = "2021-01-01"

# --------------------------------------------------------------------------------------------------
# State model — the complete ARM ``properties.state`` enum, classified into the four values the portal
# contract exposes. Anything transitional means a request is already in flight and a second one must
# not be issued.
# --------------------------------------------------------------------------------------------------
LIVE_STATES = {"Succeeded"}
PAUSED_STATES = {"Paused", "Suspended"}
TRANSITIONAL_STATES = {
    "Provisioning", "Updating", "Suspending", "Pausing", "Resuming", "Preparing", "Scaling",
}
FAILED_STATES = {"Failed", "Deleting"}

Status = Literal["live", "paused", "transitioning", "failed"]


@dataclass(frozen=True)
class CapacityState:
    """Normalised, browser-safe view of the capacity."""

    status: Status
    raw_state: str
    provisioning_state: Optional[str] = None

    @property
    def embeddable(self) -> bool:
        """True only when reports will actually render (i.e. the capacity is live)."""
        return self.status == "live"


class CapacityBusy(Exception):
    """An operation is already in flight. The web layer maps this to HTTP 409 (client keeps polling)."""


class CapacityError(Exception):
    """ARM rejected the call or the capacity is in a bad state. The web layer maps this to HTTP 502."""


def classify(raw_state: str, provisioning_state: Optional[str]) -> Status:
    """Map an ARM ``(state, provisioningState)`` pair to the portal-facing status. Pure + total."""
    # A resume that FAILED leaves the capacity in Suspended/Paused — indistinguishable from a deliberate
    # pause unless provisioningState is also read. Surface it as `failed` (needs manual intervention),
    # not `paused`, so the portal never tells users to "press Start again" forever.
    if raw_state in PAUSED_STATES and provisioning_state in FAILED_STATES:
        return "failed"
    if raw_state in LIVE_STATES:
        return "live"
    if raw_state in PAUSED_STATES:
        return "paused"
    if raw_state in TRANSITIONAL_STATES:
        return "transitioning"
    if raw_state in FAILED_STATES:
        return "failed"
    # Unrecognised value: treat as transitional (never crash), but log so ops notices a new ARM state.
    log.warning("pbie.capacity: unrecognised ARM state %r (provisioning=%r) -> transitioning",
                raw_state, provisioning_state)
    return "transitioning"


class CapacityClient:
    """Async ARM client for one Power BI Embedded capacity. Build ONCE (in the app lifespan) and reuse.

    Config is passed in (not read from env here) so the module stays framework/config-agnostic; the
    web layer constructs it from ``settings.PBIE_*``. httpx + azure-identity are imported lazily in
    ``__init__`` so importing this module (and running ``_selftest``) never requires them installed.
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        subscription_id: str,
        resource_group: str,
        capacity_name: str,
        timeout: float = 30.0,
    ) -> None:
        import httpx
        from azure.identity.aio import ClientSecretCredential

        # azure-identity caches/refreshes the ARM token internally — do NOT add a hand-rolled cache.
        self._credential = ClientSecretCredential(
            tenant_id=tenant_id, client_id=client_id, client_secret=client_secret,
        )
        self._resource_url = (
            f"{ARM}/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
            f"/providers/Microsoft.PowerBIDedicated/capacities/{capacity_name}"
        )
        self._client = httpx.AsyncClient(timeout=timeout)

    async def _headers(self) -> dict:
        token = await self._credential.get_token(ARM_SCOPE)
        return {"Authorization": f"Bearer {token.token}"}

    @staticmethod
    def _arm_error(resp) -> str:
        # Surface ARM's own error.message into logs/response — never swallow it. Different messages
        # (expired secret vs missing role vs bad resource) have different fixes.
        try:
            return resp.json()["error"]["message"]
        except Exception:
            return f"HTTP {resp.status_code}"

    async def get_state(self) -> CapacityState:
        """Read current capacity state. Cheap; safe for the portal to poll every ~10s."""
        resp = await self._client.get(
            self._resource_url, params={"api-version": API_VERSION}, headers=await self._headers(),
        )
        if not resp.is_success:
            raise CapacityError(self._arm_error(resp))
        props = resp.json().get("properties", {})
        raw = props.get("state", "unknown")
        provisioning = props.get("provisioningState")
        return CapacityState(status=classify(raw, provisioning), raw_state=raw, provisioning_state=provisioning)

    async def _invoke(self, action: Literal["suspend", "resume"], actor: str) -> CapacityState:
        """POST the action and return immediately.

        ARM answers 202 Accepted, not a completed result — the capacity is unavailable for the duration
        and the caller polls ``get_state()``. Never block here waiting for the transition to finish.
        """
        current = await self.get_state()

        if current.status == "transitioning":
            raise CapacityBusy(f"Capacity is {current.raw_state}; wait for it to settle.")
        if current.status == "failed":
            raise CapacityError(
                f"Capacity is in a failed state ({current.raw_state}/{current.provisioning_state}); "
                "needs manual inspection in the Azure portal."
            )

        # Idempotent no-ops: a double-click or a stale UI must not error and must not issue an ARM POST.
        if action == "suspend" and current.status == "paused":
            return current
        if action == "resume" and current.status == "live":
            return current

        resp = await self._client.post(
            f"{self._resource_url}/{action}", params={"api-version": API_VERSION},
            headers=await self._headers(),
        )
        # A 409 from ARM means an op is already in flight (a race after our pre-check) — keep polling,
        # do not surface it as a hard error.
        if resp.status_code == 409:
            raise CapacityBusy(self._arm_error(resp))
        if not resp.is_success:  # is_success covers 200 AND 202 (2xx) — do not require 200
            raise CapacityError(self._arm_error(resp))

        # Audit trail: a billing-affecting action on shared infrastructure — record who/from-what/outcome.
        log.info("pbie.capacity.%s actor=%s from_state=%s http=%s",
                 action, actor, current.raw_state, resp.status_code)
        return await self.get_state()

    async def pause(self, actor: str) -> CapacityState:
        """Stop the capacity and stop the meter. Embedded reports go dark."""
        return await self._invoke("suspend", actor)

    async def resume(self, actor: str) -> CapacityState:
        """Start the capacity. Not instant — the portal polls until status == 'live'."""
        return await self._invoke("resume", actor)

    async def aclose(self) -> None:
        """Release the HTTP client and the credential. Called from the app lifespan shutdown."""
        await self._client.aclose()
        await self._credential.close()


# --------------------------------------------------------------------------------------------------
# classify() self-check — pure, no framework, no network, no azure-identity. Run:
#     python app/Utils/pbie_capacity.py
# Covers all twelve ARM enum values, the Suspended/Paused + Failed edge case, and an unrecognised value.
# --------------------------------------------------------------------------------------------------
def _selftest() -> None:
    cases = [
        # (raw_state, provisioning_state, expected)  — the 12 documented ARM states:
        ("Succeeded", "Succeeded", "live"),
        ("Paused", "Succeeded", "paused"),
        ("Suspended", "Succeeded", "paused"),
        ("Provisioning", None, "transitioning"),
        ("Updating", None, "transitioning"),
        ("Suspending", None, "transitioning"),
        ("Pausing", None, "transitioning"),
        ("Resuming", None, "transitioning"),
        ("Preparing", None, "transitioning"),
        ("Scaling", None, "transitioning"),
        ("Failed", "Failed", "failed"),
        ("Deleting", None, "failed"),
        # edge case: a FAILED resume leaves Suspended/Paused + provisioningState=Failed -> failed, not paused
        ("Suspended", "Failed", "failed"),
        ("Paused", "Failed", "failed"),
        # defensive: an unrecognised ARM value must classify as transitional, never crash
        ("SomeFutureAzureState", None, "transitioning"),
    ]
    failures = []
    for raw, prov, expected in cases:
        got = classify(raw, prov)
        ok = got == expected
        if not ok:
            failures.append((raw, prov, expected, got))
        print(f"  {'OK  ' if ok else 'FAIL'} classify({raw!r}, {prov!r}) = {got!r}  (expected {expected!r})")

    # embeddable is true ONLY when live
    assert CapacityState(classify("Succeeded", "Succeeded"), "Succeeded", "Succeeded").embeddable is True
    assert CapacityState(classify("Paused", "Succeeded"), "Paused", "Succeeded").embeddable is False
    assert CapacityState(classify("Suspended", "Failed"), "Suspended", "Failed").embeddable is False

    if failures:
        raise SystemExit(f"\n{len(failures)} classify case(s) FAILED: {failures}")
    print(f"\nAll {len(cases)} classify cases passed; embeddable checks passed.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    _selftest()
