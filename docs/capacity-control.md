# Power BI Embedded — capacity start/stop control

Operator-facing control that **starts and stops the Azure Power BI Embedded capacity `a1azure`** so it
does not bill around the clock in a sandbox with no production users. core-api owns a **dedicated Azure
service principal** and exposes three endpoints; the portal calls them. The browser never talks to
Azure, and no Azure credential is ever sent to the client.

## Architecture

```
Portal UI (button + status)
      │  same-origin, session cookie
      ▼
Portal backend  ── proxies, holds the core-api credential server-side, enforces the operator allowlist
      │  X-Service-Token  OR  X-Api-Key (scope capacity:admin)  +  X-Actor: <operator>
      ▼
core-api  /api/v1/capacity/*   ── authorize("capacity:admin")
      │  ClientSecretCredential (azure-identity, built once in the app lifespan)
      ▼
Azure Resource Manager  (dedicated SPN, custom role: read / suspend / resume only)
```

The operation is **asynchronous**: ARM answers `202 Accepted`, not a completed result. The POST fires
the ARM call and returns immediately; the portal **polls `GET /state` every ~10 s** until the status
settles. No request handler and no worker ever blocks waiting for the capacity to transition.

Implementation: `app/Utils/pbie_capacity.py` (framework-agnostic async ARM client) + `app/Routers/Capacity.py`
(endpoints). The client is built once in the `middlewares.py` lifespan and hung on `app.state.capacity`.

## Endpoints

Base path is the platform default `/api/v1` (FastAPI `root_path`). All three return the standard
`DefaultResponse` envelope — the portal reads `.data`.

| Method + path | Purpose |
|---|---|
| `GET  /api/v1/capacity/state`  | Poll target for the button |
| `POST /api/v1/capacity/pause`  | Stop the capacity (suspend) |
| `POST /api/v1/capacity/resume` | Start the capacity (resume) |

**Auth (either):** `X-Service-Token` (master, full access) **or** `X-Api-Key: <prefix>.<secret>` whose
scopes include `capacity:admin`. **Audit:** send `X-Actor: <operator>` on the POSTs — the acting operator
(the portal knows who; core-api does not) is written to the audit log.

**Response body (`data`):**
```json
{ "status": "live" | "paused" | "transitioning" | "failed",
  "embeddable": true,
  "_raw_state": "Succeeded",
  "_provisioning_state": "Succeeded" }
```
`embeddable` is `true` only when `status == "live"`. `_raw_state` / `_provisioning_state` are the raw ARM
values — for logs and support, not for end-user copy.

**Status codes:**

| Code | Condition |
|---|---|
| 200 | Success, including idempotent no-ops (pausing an already-paused capacity issues no ARM POST) |
| 401 | No / invalid credentials (from `authorize()`) |
| 403 | Authenticated but the credential lacks `capacity:admin` |
| 409 | An operation is already in flight — the client should keep polling, **not** treat it as an error |
| 502 | ARM rejected the call or is unreachable (expired secret, missing role, bad resource, outage) |
| 503 | Capacity control is not configured on this server (`PBIE_*` unset / `azure-identity` missing) |

## Configuration

Six environment variables (see `.env.example`). **The feature stays disabled — endpoints return 503, the
rest of the API boots normally — until all six are set.** `PBIE_CLIENT_SECRET` is a bearer credential for
the capacity: inject via your secret manager, never commit it.

| Var | Value |
|---|---|
| `PBIE_TENANT_ID` | Entra directory (tenant) ID |
| `PBIE_CLIENT_ID` | App registration (client) ID of the **control** SPN (`svc-pbie-capacity-control`) |
| `PBIE_CLIENT_SECRET` | Client secret of that SPN |
| `PBIE_SUBSCRIPTION_ID` | `a0c800bf-9bfc-4379-a5ba-4b9ab808d744` |
| `PBIE_RESOURCE_GROUP` | `RG-UAE-PROD-PBE` |
| `PBIE_CAPACITY_NAME` | `a1azure` |

Dependency: `azure-identity` (in `requirements.txt`; picked up on image rebuild). Do **not** add
`azure-mgmt-powerbidedicated` — heavy, with blocking pollers.

## Enabling it on an environment

1. **Azure side** — run `.misc/azure_setup.sh` (needs *Application Developer* in Entra **and** *Owner* or
   *User Access Administrator* on the RG — Contributor cannot create role assignments). It creates the
   dedicated SPN, a least-privilege custom role (read / suspend / resume only — no write, scale, delete),
   and assigns it **at the capacity resource**.
2. **Secret** — `az ad app credential reset --id <appId> --years 1 --query password -o tsv` straight into
   the secret store. Shown once. Never lands in chat, a ticket, a wiki, or version control.
3. **core-api env** — set the six `PBIE_*` values; redeploy.
4. **Portal credential** — mint an API key with scope `capacity:admin` (via `/tokens`, requires
   `tokens:admin`) for the portal backend, or let it use the master `X-Service-Token`.

Verify before wiring the portal:
```
az login --service-principal -u <appId> -p <secret> --tenant <tenantId>
az rest --method get --url '/subscriptions/<sub>/resourceGroups/RG-UAE-PROD-PBE/providers/Microsoft.PowerBIDedicated/capacities/a1azure?api-version=2021-01-01' --query properties.state
```
Expect `"Succeeded"` (running) or `"Paused"` (stopped). A `403` here means the role assignment has not
propagated — wait and retry.

## Who can use it

Any caller whose credential carries `capacity:admin` (or the master service token). **Which humans** may
control the capacity is decided by the **portal** — it authenticates the operator, checks its own operator
list, and passes the operator via `X-Actor`. core-api deliberately does not keep a second allowlist.

## Troubleshooting — the codes have different fixes

Distinguishing 401 / 403 / 502 is the whole point; do not collapse them.

| You get | It means | Fix |
|---|---|---|
| **401** | No / invalid `X-Service-Token` / `X-Api-Key` reached core-api | Portal backend is not sending the credential, or it is wrong/rotated |
| **403** | Credential is valid but has no `capacity:admin` scope | Add the scope to the API key (or use the master token) |
| **409** | An operation is already in flight | Not an error — keep polling `GET /state` |
| **502** | ARM rejected/unreachable. The log line carries ARM's `error.message` | **Expired secret** → rotate (see below). **Missing role assignment / unpropagated role** → do NOT widen the role; re-check/await the assignment. Outage → retry |
| **503** | `PBIE_*` not fully set, or `azure-identity` not installed | Finish "Enabling it" above |

**Audit log** — every state-changing call logs the actor, the prior state and the outcome (it is a
billing-affecting action on shared infrastructure). Find them with:
```
grep 'capacity\.\(pause\|resume\)' <core-api logs>       # gateway audit line (actor + correlation id + result)
grep 'pbie\.capacity\.'            <core-api logs>       # ARM-level line (from_state + http status)
```

## Credential ownership & rotation

A client secret is the pragmatic default but **expires** (Entra caps it at 24 months; tenant policy is
usually shorter). When it lapses the button stops working with a `502` whose log line identifies it as an
**authentication failure against ARM** — on a control nobody has touched in weeks. Calendar the rotation.

Rotate: `az ad app credential reset --id <appId> --years 1 --query password -o tsv` → secret store →
update `PBIE_CLIENT_SECRET` → redeploy. (Or switch to `CertificateCredential` — a two-line change in
`app/Utils/pbie_capacity.py`.)

| | Value |
|---|---|
| Control SPN | `svc-pbie-capacity-control` (appId: _fill in_) |
| Secret owner | **_fill in — a named person_** |
| Secret created | _fill in_ |
| **Secret expires** | **_fill in — set a calendar reminder ~1 month before_** |
