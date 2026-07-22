# AIXII platform — operations

How to run, configure, scale and operate the three services. See
[architecture.md](architecture.md) for how they fit together.

## Prerequisites
- ONE shared **PostgreSQL** (the `pgvector/pgvector:pg16` image; the core DBs created once) and
  ONE shared **Redis** reachable by all services. For a self-hosted pair see
  `infra/docker-compose.yml`.
- Each service has its OWN `.env` (copy from `.env.example`). They do NOT share a file, but the
  DB/Redis connection vars and the shared secrets MUST match across services.

## Running a service

### Docker (normal path)
```bash
cp .env.example .env          # fill DB_*/REDIS_* and the secrets below
docker compose up -d --build
```
- core-api: serves on `:8000` (health `GET /api/v1/health/` via the configured root path).
- file-processor: host `:8001` → container `:8000`.
- external-worker: no port; healthcheck greps the `arq` process.

Point `DB_HOST` / `REDIS_HOST` at the SHARED cluster. The service compose files do NOT start
their own Postgres/Redis.

### Locally (no Docker)
Imports inside each service are bare, so set the package dir on `PYTHONPATH` (the images set it):
```bash
# core-api
PYTHONPATH=app python app/main.py
# external-worker
cd worker && PYTHONPATH=. arq main.WorkerSettings
# file-processor
PYTHONPATH=worker python worker/main.py
```
`DEV_MODE=true` loads `.env.dev` instead of `.env`.

## Configuration (key env vars)

### Shared by all services
| Var | Meaning |
|-----|---------|
| `DB_USER`,`DB_PASSWORD`,`DB_HOST`,`DB_PORT` | Postgres connection |
| `DB_NAME` | comma list of real DB names; logical names (`main`,`service`,`cirium`,…) resolve by substring |
| `REDIS_HOST`,`REDIS_PORT`,`REDIS_USER`,`REDIS_USER_PASSWORD` | Redis connection (cache + ARQ broker + pub/sub) |
| `DEV_MODE` | `true` → load `.env.dev`, DEBUG logging |
| `LOG_LEVEL` | override log level: `DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL` (else DEBUG in dev, INFO in prod) |

### core-api
| Var | Meaning |
|-----|---------|
| `SERVICE_TOKEN` | master shared secret (trusted internal callers; full access). Empty ⇒ token-protected routes deny all |
| `API_TOKEN_PEPPER` | server-side pepper mixed into every API-key hash. Set a long random value; rotating it invalidates ALL issued keys |
| `FILE_PROCESSOR_URL`,`FILE_PROCESSOR_TOKEN` | where to forward uploads (must equal file-processor `SERVICE_TOKEN`) |
| `MS_WEBHOOK_SECRET` | Microsoft Graph webhook validation (must equal external-worker) |
| `CORS_ORIGINS`,`CORS_CREDENTIALS`,… | CORS (see the note at the bottom) |
| `API_ROOT_URL` | API path prefix (default `/api/v1`) |

### external-worker
| Var | Meaning |
|-----|---------|
| `SCHEDULER_ENABLED` | run the schedule dispatcher tick. **Exactly ONE replica** must set this `true` |
| `MAX_JOBS` | concurrent jobs per process (async fan-out; default 10) |
| `USE_PROCESS_POOL`,`PROCESS_WORKERS` | optional CPU-bound process pool (default on; size = CPU count) |
| `MS_*`,`AIRLABS_API_KEY`,`FLIGHT_RADAR_API_KEY`,`AVIATION_EDGE_*` | external API credentials (required — `require_env` raises at import if missing) |

### file-processor
| Var | Meaning |
|-----|---------|
| `SERVICE_TOKEN` | must equal core-api `FILE_PROCESSOR_TOKEN` |
| `FP_WORKERS` | concurrent **groups** processed at once per replica (default 2) |
| `FP_INSTANCE_ID` | stable per-replica id for crash recovery (else container hostname) — see scaling |
| `FP_USE_PROCESS_POOL`,`FP_PROCESS_WORKERS` | CPU-bound process pool (default on; size = CPU count) |

## Migrations (core-api only)
Always use the per-DB runner — never bare `alembic`:
```bash
python tools/migrate.py upgrade service head    # apply service-DB migrations
python tools/migrate.py current service
python tools/migrate.py revision service "msg"  # autogenerate (then hand-edit DROPs — env.py
                                                # skips tables not in metadata)
```
Recent service-DB migrations to apply: the cleanup + `schedule_registry` (`b7e1c2d3a4f5`) and
`api_tokens` (`c3d4e5f6a7b8`). Valid DB keys: `main service cirium airlabs flightradar (fr)
aviationedge (ae)` (PowerPlatform was removed).

## Running several replicas on different servers

All three services are **stateless** — every bit of shared state lives in the shared Postgres
and Redis. So you scale horizontally by running more identical containers that all point at the
SAME `DB_HOST`/`REDIS_HOST`. The only constraints:

### core-api — scale freely
Put N replicas behind a load balancer. No special config; each is identical. (Long-lived SSE
`/status/stream` connections are per-replica, which is fine.)

### external-worker — scale freely, ONE scheduler
- Run N replicas against the same Redis. The ARQ in-progress lock makes each replica pull
  DISJOINT jobs, so on-demand work simply parallelises; total concurrency = N × `MAX_JOBS`.
- Set `SCHEDULER_ENABLED=true` on **exactly one** replica so scheduled jobs aren't multiplied.
  (ARQ's unique cron job-id also dedups the tick, but keep it single-owner to be safe.)
- Raise `MAX_JOBS` for more per-process async fan-out (IO-bound); add replicas for more cores.

### file-processor — scale freely, give each replica a stable id
- Run N replicas; they all share the ONE `fp-workers` consumer group, so per-user groups
  distribute across replicas (cross-group concurrency = Σ `FP_WORKERS`). FIFO-within-a-group and
  no-interleave still hold because the per-group **lease** allows only one worker per group at a
  time across the whole fleet.
- Set a **stable `FP_INSTANCE_ID` per replica** (e.g. the StatefulSet pod name, or a fixed id per
  compose service). Crash recovery re-injects a replica's in-flight file by its consumer id, so a
  replica that restarts with the same id recovers its own work; a replica that vanishes for good
  has its queued work re-rung by the reclaim sweeper, but its single in-flight file is only
  recovered if it comes back with the same id. **Ingest should be idempotent** (at-least-once on
  crash).
- Requires Redis ≥ 6.2 (LMOVE).

## Control-plane admin APIs (core-api)

All under `API_ROOT_URL` (default `/api/v1`). Authorise with `X-Service-Token` (full access) or an
`X-Api-Key` holding the listed scope.

### Scheduler — `/scheduler`  (scopes: `scheduler:read` / `scheduler:write`)
```
GET    /scheduler                 list all schedules + state
GET    /scheduler/{name}          one schedule
PATCH  /scheduler/{name}          {enabled?, paused?, interval_seconds?, cron_expr?, kwargs?}
POST   /scheduler/{name}/run      force an immediate run (observe via /status/{job_id})
DELETE /scheduler/{name}          remove a schedule row (decommission a job)
```
A schedule is interval-driven (`interval_seconds`) XOR cron-driven (`cron_expr`). Changes take effect
within one dispatcher tick (~1 min).

**Mechanics you must know before editing schedules:**
- **`cron_expr` is evaluated in UTC** (the worker advances `next_run_at` from `datetime.now(timezone.utc)`).
  The ops target is Asia/Dubai (UTC+4, no DST): a Dubai hour `T` → cron hour `T − 4`. E.g. Mon 07:00 Dubai
  = `"0 3 * * 1"`.
- **Seeding is insert-if-absent.** Workers seed a default row per schedulable job on startup with
  `on_conflict_do_nothing`, so **editing a worker's `SCHEDULE_DEFAULTS` never changes an existing row** —
  only the very first seed, or a fresh DB. To change a live schedule use **PATCH** (or DELETE + re-seed).
- **PATCH resets `next_run_at`** when it changes `cron_expr`/`interval_seconds`, so the new cadence takes
  effect on the correct upcoming cycle (a cron row re-initialises to its next fire without firing now).
- **DELETE is the only way to drop a decommissioned job:** a stale row whose name is no longer in any
  worker's `SCHEDULE_DEFAULTS` is never GC'd and keeps dispatching. Remove the default from the worker
  first (else it re-seeds on the next worker restart), then DELETE the row.

**Current Cirium Monday-morning chain** (all UTC; Dubai = +4). The scraper robot drops fresh Cirium files,
then the refresh jobs process them:

| Job (`func_name`) | cron (UTC) | Dubai | Queue |
|---|---|---|---|
| `cron_scrape_cirium` (scraper robot) | `0 1 * * 1` | Mon 05:00 | `core:robot` |
| `cron_collapse_revisions` | `0 2 * * *` | daily 06:00 | `core:external` |
| `cron_asg_regs` | `0 3 * * 1` | Mon 07:00 | `core:external` |
| `cron_refresh_delta` | `30 3 * * 1` | Mon 07:30 | `core:external` |
| `cron_refresh_plantype_matviews` | `0 4 * * 1` | Mon 08:00 | `core:external` |

`cron_scrape_cirium` is dispatched onto the dedicated **`core:robot`** queue, consumed by a **separate
scraper-robot service** (its own repo — see `.misc/CIRIUM_ROBOT_AGENT_PROMPT.md`). External-worker seeds
this row **paused**; unpause it (`PATCH … {"paused": false}`) once the robot is deployed and consuming
`core:robot`, else the weekly job piles up with no consumer.

### Queues — `/queues`  (scope: `queues:admin`)
```
GET    /queues                    depth + paused flag for each ARQ queue
POST   /queues/{external|files}/pause     stop the dispatcher enqueuing to it (cooperative)
POST   /queues/{external|files}/resume
POST   /queues/{external|files}/purge     drop all queued (not-yet-started) jobs
```
(file-processor's own `fp:process` queue is paused with the `fp:paused` Redis key.)

### API tokens — `/tokens`  (scope: `tokens:admin`; the master service token always qualifies)
```
POST   /tokens     {name, scopes:[...], expires_at?}  → returns the full key "ak_….<secret>" ONCE
GET    /tokens     list (never returns secrets/hashes)
PATCH  /tokens/{prefix}   {enabled?, scopes?, name?, expires_at?}  (send expires_at:null to clear)
DELETE /tokens/{prefix}   revoke
```
Domain scopes: `flights:read`, `status:read`, `files:write`, `scheduler:read`, `scheduler:write`,
`queues:admin`, `tokens:admin`, and `admin` (superscope). A non-`admin` token holder cannot grant
scopes it does not itself hold.

Issue a key, hand it to the caller, who then sends it as `X-Api-Key: ak_….<secret>`. To protect a
data endpoint with it, add one dependency, e.g.:
```python
from api_auth import authorize, SCOPE_FLIGHTS_READ
@router.get("/flightsummary", dependencies=[Depends(authorize(SCOPE_FLIGHTS_READ))])
```
(The existing `/flightradar` and `/status` endpoints are intentionally left open — gating them is
an opt-in to coordinate with current callers like PowerBI / the portal.)

## Logging
- Level via `LOG_LEVEL` (per env). Format: `LEVEL: [name] time | file-line: message`.
- Console always; a rotating daily file (`Logs/LOG_YYYY-MM-DD.log`, 10 MiB ×5, zipped) in the
  MAIN process only — process-pool children log to console (captured by the container) to avoid
  rotation races.
- core-api stamps an `X-Correlation-ID` per request and includes it in error log lines; clients
  get that id on errors (never the exception internals).

## Notes / recommendations
- **CORS**: the default `CORS_ORIGINS=*` with `CORS_CREDENTIALS=true` is rejected by browsers for
  credentialed requests — set explicit origins before exposing credentialed/admin endpoints.
- **Idempotency**: the file queue and ARQ are at-least-once on crash; make ingest idempotent.
- **db-contract sync**: after changing a model, copy it from `db-contract/Database/` into
  `app/Database/` and each worker's `worker/Database/` (no auto-sync; `ServiceModels.py` must stay
  byte-identical across all four).
