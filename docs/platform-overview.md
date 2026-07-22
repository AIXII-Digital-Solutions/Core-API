# AIXII platform — orientation map (start here)

**Read this first.** It is the single entry point for anyone (human or agent) new to the platform. It
says **what exists, where it lives, what each part is for, and why** — so you can navigate straight to the
right file instead of re-reading the whole codebase. It is deliberately a *map*, not a deep dive: for
depth follow the links to the focused docs.

- Platform architecture & contracts: [architecture.md](architecture.md)
- Running / configuring / env reference / control-plane APIs: [operations.md](operations.md)
- Power BI Embedded capacity control: [capacity-control.md](capacity-control.md)
- Agent/Claude-Code repo rules for core-api: [`../CLAUDE.md`](../CLAUDE.md)
- Schema source of truth: [`../db-contract/`](../db-contract)

---

## 1. Why this platform exists

AIXII is an **aviation data platform**. It ingests aircraft/fleet and flight data from several external
providers, normalises it into one PostgreSQL cluster, and serves it to downstream consumers (the **portal**,
Power BI reports, external API-key holders). On top of the raw data it runs an **ACYS forecast model** that
projects future fleet utilisation (aircraft-cycles / hours) per operator. Everything an operator can trigger
from the portal — file uploads, scheduled refreshes, forecasts, Power BI capacity start/stop — flows through
this platform.

## 2. The four repositories (siblings under `G:\Projects\`)

Three independently-deployable **services** + one shared **schema contract**. They share ONE Postgres cluster
and ONE Redis. Only core-api is exposed to the internet.

| Repo | Role | Runtime | Entry point |
|---|---|---|---|
| **`Core-API`** | Public FastAPI **gateway** + the platform's only Alembic (owns the DB schema) + control plane | HTTP-only, stateless, scale by replicas | `app/main.py` → `app/Server.py` |
| **`External-Worker`** | **ARQ worker**: all external-API integration (FR24 / Airlabs / Aviation Edge / MS Graph) + scheduled domain jobs + the **forecast engine** | ARQ over Redis, no HTTP | `worker/main.py` (`WorkerSettings`) |
| **`File-Processor`** | **File ingestion** service: receives uploads from core-api, parses CSV / Excel / Cirium into Postgres | FastAPI + per-user Redis queue | `worker/main.py` → `worker/server.py` |
| **`db-contract`** | **Schema source of truth** (ORM models + forecast spec). Not a service — lives *inside* `Core-API/db-contract/` and is read by Alembic | n/a (library) | `db-contract/Database/` |

**Golden rule — vendored copies, no shared package.** To stay independently deployable, each service carries
its **own copy** of the ORM models (`Database/`), `Config/`, `status.py`, `Queue.py`, and the forecast spec.
There is **no auto-sync**. A schema/spec change is a manual multi-repo edit:
1. edit `db-contract/Database/` (what Alembic reads),
2. generate + apply the migration (`tools/migrate.py`),
3. copy the changed model file into `Core-API/app/Database/` **and** each worker's `worker/Database/`.
`ServiceModels.py` (`job_statuses` / `schedule_registry` / `api_tokens`) must stay byte-identical in all four
locations. The forecast spec has three copies — see §6.

## 3. How they talk (the contracts)

```
 portal / PowerBI / API-key ──HTTP──▶ core-api ──┬─ ARQ enqueue (core:external) ─▶ external-worker
                                                 ├─ HTTP POST /process (X-Service-Token) ─▶ file-processor
                                                 └─ reads ◀── job_statuses table + Redis status:events
```

| Contract | What it is | Defined in |
|---|---|---|
| `core:external` (ARQ queue) | core-api → external-worker job dispatch (by worker function name) | `Queue.py` (both) |
| `core:robot` (ARQ queue) | external-worker scheduler → **scraper-robot service** (`scrape_cirium`); robot's own repo | `Queue.py` `ROBOT_QUEUE` |
| HTTP `POST /process` | core-api → file-processor upload forward; `X-Service-Token`; form `kind`/`job_id`/`group` | `app/Routers/Files.py` → fp `worker/server.py` |
| `job_statuses` table (service DB) | durable per-job status; workers UPSERT on `job_id` | `db-contract/Database/ServiceModels.py` |
| `status:events` (Redis pub/sub) | compact live status JSON; core-api relays it as SSE | workers' `status.py` → `app/Routers/StatusCheck.py` |
| `schedule_registry` table | runtime cron control plane (core-api writes, worker reads) | `ServiceModels.py` |
| `api_tokens` table | gateway API keys (sha256 hash + scopes) | `ServiceModels.py` |

Full keyspace / channel details: [architecture.md](architecture.md).

## 4. The databases (owned by core-api)

After the **AIXII consolidation** there are exactly **two physical Postgres databases**:
- **`aixii`** — every aviation domain is a **schema**: `cirium`, `airlabs`, `flightradar`, `aviationedge`,
  plus `forecast` and a temporary `main`. Tables emit as `cirium.aircrafts`, `flightradar.livepositions`, …
- **`service`** — schema-less: `job_statuses`, `schedule_registry`, `api_tokens`, `forecast_profiles`,
  `forecast_last_requests`, `forecast_step_timings`.

**Logical → physical routing:** call sites pass logical names (`main`/`cirium`/`airlabs`/`flightradar`/
`aviationedge`/`service`); `DBSettings.physical_db()` maps all aviation names to `aixii`, so the five share
ONE pooled engine; routing to the right table is by the model's **schema**, not the session.

**core-api is the only Alembic.** Two revision trees, driven per-DB (never bare `alembic`):
`migration/versionsAixii/` and `migration/versionsService/`, via `python tools/migrate.py <action> <aixii|service> …`.
Provision the cluster with `docs/db-aixii-setup.sql` first. Deep notes live in `migration/env.py` +
`tools/migrate.py` comments (the `version_locations` gotcha).

> **`main`/core is being rewritten and is intentionally NOT migrated** — schema-less, excluded from aixii
> metadata. Some FR24/Airlabs/CSV paths that read those tables fail at query time until core is rebuilt.
> `main.virtual_airport_list` is a temporary copy for the worker's distance calc; remove at core rebuild.

## 5. Core-API map (`app/`)

The working directory. FastAPI gateway; does **no** background work of its own.

| Area | Path | What |
|---|---|---|
| Boot | `app/main.py`, `app/Server.py`, `app/middlewares.py` | uvicorn launcher; app factory (`root_path=/api/v1`, auto-mounts routers); lifespan builds `app.state` singletons (redis, `db_client`, `db_proxy`, arq pool, capacity client) |
| Routers | `app/Routers/` | one file per domain; **must be exported in `Routers/__init__.py`** to mount. Files: `Root`, `Health`, `FlightRadar`, `Airlines`, `Registrations`, `Database`, `Forecast`, `Files`, `Webhook`, `StatusCheck`, `Scheduler`, `QueueAdmin`, `Tokens`, `Capacity` |
| Auth | `app/api_auth.py` (`authorize(*scopes)`), `app/service_auth.py` | `X-Service-Token` (master) OR `X-Api-Key` (scoped). Scope list + `ALL_SCOPES` here |
| DB access | `app/Database/Client.py` (`DatabaseClient.session`), `app/Utils/Middlewares.py` (`DBProxy` = DB + Redis read-through cache, `@cache_query`) | two deliberate patterns — pick per use |
| Responses | `app/Utils/ResponsesFunc.py` | `success_/warning_/error_response` → `DefaultResponse` envelope + correlation id |
| Config | `app/Config/config.py` (shared: env, `DBSettings`, logging) + `app/settings.py` (this segment: server, CORS, paths, the custom `Router` subclass that registers `/x` **and** `/x/`) | `DEV_MODE=true` loads `.env.dev` |
| Queue helpers | `app/Queue.py` (queue names), `app/enqueue.py` (`enqueue_external`/`enqueue_file`) | |
| Control plane | `Routers/Scheduler.py` (CRUD `schedule_registry`), `Routers/QueueAdmin.py` (inspect/pause/purge ARQ), `Routers/Tokens.py` (mint/revoke API keys) | |
| Capacity | `app/Utils/pbie_capacity.py` (async ARM client) + `Routers/Capacity.py` | Power BI Embedded start/stop — see [capacity-control.md](capacity-control.md) |
| Schema owner | `db-contract/` (ORM + forecast spec), `migration/` (two trees), `tools/migrate.py` | |
| One-off ops | `_admin/` | `load_historical_cirium.py`, `collapse_revisions.py`, OurAirports CSV loaders (dry-run by default) |

## 6. The forecast subsystem (spans three repos)

The **ACYS forecast** projects operator fleet utilisation. This is the most cross-cutting feature.

- **Engine (production):** `External-Worker/worker/API/ForecastAPI/` — ARQ job **`forecast_panel`**.
  - `panel.py` — orchestrator: assembles `forecast.acys_actuals` from **Cirium × FR24** (date-respecting per
    operator), merges into summary tables, publishes per-step status. Bounded FR24 fetch (`FORECAST_FETCH_BUDGET_SECONDS`).
  - `model.py` — the model: reads `acys_actuals`, writes `acys_forecast` (sub-fleet **level × seasonal ×
    fleet-growth**, cascading route-pool tiers).
- **Research/offline lab:** `External-Worker/predictive/` — backtest + LOCO validation; **not** imported by the
  running worker. Design rationale in `predictive/README.md` and `External-Worker/docs/forecast_model_spec.md`.
- **Runtime knobs (portal-configurable):** stored in `service.forecast_profiles.params` (JSONB overrides).
  core-api serves `GET /forecast/params/schema` and manages profiles via `/forecast/profiles`.
- **The spec — THREE byte-identical copies** (`diff` them on any change):
  `db-contract/forecast_params.py` (source) → `Core-API/app/Utils/forecast_params.py` (validate on write) →
  `External-Worker/worker/API/ForecastAPI/params.py` (resolve on read). Adding a knob = add to `SPEC` with a
  default equal to today's behaviour, copy, read `p.<name>` — no migration, no portal release. Incompatible
  change bumps `MODEL_VERSION`.
- **Report matviews:** `forecast.grouped_by_reg` / `aircraft_information` / `z_dates_acys` (+ others),
  refreshed at the end of the panel job by dependency order.

## 7. The Cirium pipeline (ingest → collapse → matviews → forecast)

Cirium is the primary fleet-reference dataset; understanding its flow explains most of the aviation schema.

0. **Scrape** (external **scraper-robot service**, its own repo — brief in `.misc/CIRIUM_ROBOT_AGENT_PROMPT.md`):
   an ARQ worker on the dedicated **`core:robot`** queue running task `scrape_cirium`. External-worker's
   scheduler triggers it every **Mon 05:00 Dubai** (`0 1 * * 1` UTC); it scrapes both plans and uploads each
   file to core-api `POST /api/v1/files` (`X-Service-Token`), which forwards to File-Processor. Seeded
   **paused** until the robot is deployed.
1. **Ingest** (`File-Processor`): a `.xlsx` arrives via `POST /process` (`kind=cirium` → plan_type
   *Commercial*; `kind=cirium_business` → *Business&Helicopters*) or a watched drop folder. `process_cirium_file`
   (`worker/ingest/CiriumFiles.py`) calls `get_or_create_revision` — **one revision per (day, plan_type)**,
   collapsing multiple same-day files — parses in a process pool, and `COPY`s rows into `cirium.aircrafts`.
2. **Collapse** (`External-Worker` cron `cron_collapse_revisions`): `cirium.collapse_completed_months()`
   dedups completed-month live revisions.
3. **Matviews** (`External-Worker` crons `cron_asg_regs` / `cron_refresh_delta` / `cron_refresh_plantype_matviews`,
   logic in `worker/API/Utils/RegsListUpdater.py`): refresh `asg_*` / `delta_*` / `all_*` / `historical_*`
   (per-plan variants **before** the `_full` union, because `REFRESH … CONCURRENTLY`). Heavy SQL is owned by
   core-api Alembic; the worker jobs just drive `REFRESH`.
4. **Consume:** the forecast panel, reports, and `/scheduler run-now` read the current fleet from these views.

**Monday-morning chain** (all UTC; Dubai = +4): robot `0 1 * * 1` (Mon 05:00) → collapse `0 2 * * *`
(daily 06:00) → asg `0 3 * * 1` (07:00) → delta `30 3 * * 1` (07:30) → plantype `0 4 * * 1` (08:00).
Cron is UTC; editing a worker's `SCHEDULE_DEFAULTS` does **not** move an existing live row (seed is
insert-if-absent) — reschedule via core-api `PATCH /scheduler/{name}`. See
[operations.md](operations.md#scheduler---scheduler--scopes-schedulerread--schedulerwrite).

> Refresh runs as the matview **OWNER** (`grp_aviation_write`) — that role needs `TEMPORARY` on `aixii` for the
> concurrent-refresh temp table (granted in `docs/db-aixii-setup.sql`). Heavy refreshes exceed ARQ's 300 s
> default, so they get `CIRIUM_REFRESH_JOB_TIMEOUT_SECONDS` (see `External-Worker/worker/main.py`).

## 8. External-Worker map (`worker/`)

ARQ worker, no HTTP. Boots via `worker/main.py` `WorkerSettings` (`queue_name=core:external`,
`functions=ON_DEMAND+SCHEDULED`, one cron `dispatch_due`).

| Area | Path | What |
|---|---|---|
| Job registry | `worker/tasks.py` (`ON_DEMAND`/`SCHEDULED`), `worker/main.py` (`_register_job` applies long timeouts) | |
| Scheduler | `worker/scheduler.py` | `seed_registry` (default rows on startup, **insert-if-absent** — never overwrites live edits), `dispatch_due` (per-minute tick reads `schedule_registry`, enqueues due/`run_now`, advances `next_run_at`). **`cron_expr` is UTC.** Also seeds the paused `cron_scrape_cirium` row (queue `core:robot`) for the external scraper robot |
| FlightRadar24 | `worker/API/FlightRadarAPI/` | `LiveFlightsAPI` (adaptive live poll + distance), `FlightSummary`, `AirportsAPI`, `coverage`, `distance` |
| Airlabs / Aviation Edge | `worker/API/AirlabsAPI/`, `worker/API/AviationEdgeAPI/` | |
| Microsoft Graph | `worker/API/Clients/MSGraphsClient.py`, `worker/API/MSGraphAPI/`, `…/Utils/MSGraphSubscriptionManager.py` | webhook subscription lifecycle |
| Cirium refresh | `worker/API/Utils/RegsListUpdater.py` | the four `cron_*` matview jobs (§7) + `ensure_livepositions_partitions` |
| Forecast | `worker/API/ForecastAPI/` | §6 |
| Config | `worker/settings.py` (rate limits/timeouts/webhook URLs) + shared `worker/Config/` | |

## 9. File-Processor map (`worker/`)

FastAPI + per-user Redis queue. Boots via `worker/main.py` → `worker/server.py` (`lifespan` spawns
`FP_WORKERS` queue consumers + one drop-folder watcher per kind).

| Area | Path | What |
|---|---|---|
| HTTP | `worker/server.py` | `POST /process` (service-token; saves to local `INTAKE_PATH`, enqueues, returns 202), `GET /health`; `PROCESSORS` dispatch table `kind → (fn, watch_path, ext, db)` |
| Queue | `worker/jobqueue.py` | per-group FIFO on `fp:*` keys (doorbell stream `fp:ready` + group `fp-workers`, per-group list `fp:u:{group}`, leases). At-least-once → ingest must be **idempotent** |
| Ingest | `worker/ingest/` | `CSVFiles.py` (Registrations/Airlines), `EXCELFiles.py` (sheet→table via pandas), `CiriumFiles.py` (revision + `COPY`), `FilesFinder.py` (drop-folder watcher, inline) |
| Process pool | `worker/pools.py` | `run_cpu()` for pandas parsing (true multi-core; args must be picklable — Excel passes a DSN string, not a session) |
| Status | `worker/status.py` | `publish_status` — UPSERT `job_statuses` + publish `status:events`; best-effort (never fails the job) |

## 10. Auth, responses & routing conventions (core-api)

- **Two credentials, one dependency:** `Depends(authorize("scope"))` accepts the master `X-Service-Token`
  OR an `X-Api-Key: <prefix>.<secret>` whose scopes cover the requirement. `/tokens` mints keys (scope
  `tokens:admin`). Older `service_auth.verify_service_token` still gates `Files.py`.
- **Routers auto-register** from `Routers/__init__.py`; use the custom `Router` subclass from `settings.py`
  (registers `/x` and `/x/` — no redirect). New router with `prefix="/foo"` → `/api/v1/foo/*`.
- **Responses** go through `success_/warning_/error_response` (envelope + correlation id); declare
  `responses=build_responses(...)` for OpenAPI. `warning_/error_response` return `data=[]` — never pair them
  with a parametrized `response_model=DefaultResponse[X]` (shape mismatch).

## 11. Git & deploy

- Remotes: each repo has its own. Core-API → `AIXII-Digital-Solutions/Core-API`. **Push to `dev`; `main` via
  PR only** (local `main` is push-blocked on purpose).
- **Deploy = build prod images from the LOCAL working tree** (`docker compose up -d --build`), so *uncommitted*
  edits are what runs in prod. Commit afterwards to keep history honest.
- No test suite, no linter/formatter configured — do not invent `pytest`/`ruff` commands.

## 12. Removed / in-flight (don't chase ghosts)

- **PowerPlatform is fully removed** across all repos + db-contract (routers, models, schemas, base, worker
  jobs, DB targeting, `versionsPowerPlatform`). `MainModels` was kept.
- **core/`main` DB rewrite** is pending (§4) — schema-less, not migrated; some read paths fail until rebuilt.
- Some `cirium.asg`/`delta` "3-revisions = current" matview logic is a **temporary** window; see the codebase
  comments / migration history for the revert plan.
