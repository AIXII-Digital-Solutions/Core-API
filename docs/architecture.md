# AIXII platform — architecture

Three independently-deployable services share ONE PostgreSQL cluster and ONE Redis instance.
core-api is the only public entry point; the workers are reached only through Redis/HTTP.

```
                      ┌──────────────────────────────────────────────┐
        clients  ───▶ │  core-api  (FastAPI gateway, HTTP-only)       │
   (portal, PowerBI,  │  • serves data + job status (REST + SSE)      │
    external API key) │  • owns the DB schema (Alembic / db-contract) │
                      │  • control plane: scheduler / queues / tokens │
                      └───┬───────────────┬───────────────┬──────────┘
              ARQ enqueue │   HTTP /process│        writes │ reads (status)
            (core:external)│ (X-Service-Token)            │
                          ▼               ▼               ▼
              ┌────────────────────┐  ┌────────────────────┐   ┌──────────────────────┐
              │  external-worker   │  │   file-processor   │   │  PostgreSQL cluster  │
              │  (ARQ worker)      │  │  (FastAPI + queue) │   │  ai12_main, _service │
              │  • on-demand jobs  │  │  • per-user file   │   │  cirium, flightradar │
              │  • scheduled jobs  │  │    queue (Redis)   │   │  …                   │
              │    (registry tick) │  │  • process-pool    │   └──────────────────────┘
              │  • FlightRadar/    │  │    ingestion       │   ┌──────────────────────┐
              │    Airlabs/AE/MS   │  └─────────┬──────────┘   │   Redis (shared)     │
              └─────────┬──────────┘            │              │  ARQ queues, fp:*,   │
                        │  status               │ status       │  status:events,      │
                        └───────────┬───────────┘              │  cache, leases       │
                                    ▼                          └──────────────────────┘
                        job_statuses table + Redis "status:events"  ── read back by core-api
```

## The services

### core-api — the gateway (HTTP-only)
A FastAPI app. It does **no** background work itself: it serves aviation data, dispatches jobs
to the workers, forwards uploaded files to file-processor, exposes job status (REST + live SSE),
and is the platform's single Alembic owner (schema source of truth in `db-contract/`).
It also hosts the **control plane**: `/scheduler`, `/queues`, `/tokens`.
- Entrypoint: `app/main.py` → `uvicorn` → `app/Server.py` (`app`). Routers auto-mount from
  `app/Routers/__init__.py`.
- Concurrency: a single async process; scale by running more replicas behind a load balancer
  (it is stateless — all state lives in Postgres/Redis).

### external-worker — background jobs (ARQ)
An ARQ worker (`arq worker.main.WorkerSettings`, no HTTP). Two roles on the shared Redis:
1. **On-demand** jobs that core-api enqueues onto `core:external` (flight summary, airports,
   guest invite, subscription refresh).
2. **Scheduled** jobs driven by the schedule registry (see below): a single per-minute cron
   tick (`dispatch_due`) reads `schedule_registry` and enqueues due jobs.
- It is the platform's external-API integration point: FlightRadar24, Aviation Edge, Airlabs,
  Microsoft Graph.
- Concurrency: async fan-out `MAX_JOBS` per process (IO-bound) + horizontal replicas; an
  optional process pool (`worker/pools.py`) for any CPU-bound task.

### file-processor — file ingestion (FastAPI + Redis queue)
A FastAPI service. core-api POSTs uploaded files to `POST /process` (service-token auth); it
stores them on its OWN volume (no shared filesystem) and ingests CSV / Excel / Cirium into
Postgres. It also watches local drop folders.
- Entrypoint: `worker/main.py` → `uvicorn` → `worker/server.py` (`app`).
- Concurrency: a **per-user grouped** Redis queue — within a group files are strictly FIFO and
  never interleaved with another group's; across groups up to `FP_WORKERS` run concurrently.
  CPU-bound parsing (Excel `to_sql`, Cirium parse) is offloaded to a process pool for true
  multi-core parallelism.

## How they communicate (the contracts)

None of these is a shared runtime package — each contract is a literal duplicated/vendored in
every service, so the services stay independently deployable. Do not rename the literals.

| Contract | What | Where |
|----------|------|-------|
| `core:external` | ARQ queue: core-api → external-worker (enqueue by worker function `__name__`) | `Queue.py` (both) |
| `core:files` | ARQ queue, **reserved/unused** — file flow is HTTP, not ARQ | `Queue.py` |
| `core:robot` | ARQ queue: external-worker scheduler → the standalone **Cirium scraper robot** (`scrape_cirium`, robot's own repo) | `Queue.py` `ROBOT_QUEUE` |
| HTTP `POST /process` | core-api → file-processor file forward; header `X-Service-Token`; form fields `kind`, `job_id`, `group` | `Files.py` → `server.py` |
| `job_statuses` table | durable per-job status in the `service` DB; writers UPSERT on `job_id` | `db-contract/Database/ServiceModels.py` |
| `status:events` | Redis pub/sub channel; compact JSON `{job_id,kind,ref,state,progress,message}` | workers' `status.py` → core-api `/status/stream` |
| `fp:*` | file-processor's own per-user queue keys (see below) | `file-processor/worker/jobqueue.py` |
| `schedule_registry` table | the runtime schedule control plane (core-api writes, worker reads) | `ServiceModels.py` |
| `api_tokens` table | gateway API keys (hash + scopes) | `ServiceModels.py` |

### Status flow (every background job)
1. core-api enqueues (ARQ) or forwards (HTTP) a job with a `job_id`.
2. The worker UPSERTs `job_statuses` (state `queued`→`running`→`success`/`error`) and publishes
   a compact event to `status:events`. Best-effort: a status failure never fails the job.
3. core-api serves it back: `GET /status`, `GET /status/{job_id}` (REST over the table), and
   `GET /status/stream` (SSE relaying `status:events`).

### Scheduler flow (runtime-controllable cron)
core-api OWNS `schedule_registry`; external-worker runs a one-minute `dispatch_due` tick that
reads enabled, non-paused rows whose `next_run_at` is due (or `run_now` is set), enqueues
`func_name` on its queue and advances `next_run_at`. So enabling/disabling, changing an interval
or cron expression, and forcing a run all happen at runtime with **no worker restart** — for
every existing and future schedulable job (each self-registers a default row on startup).

### Auth
- `X-Service-Token` — shared secret for fully-trusted internal backends (the portal). Full access.
- `X-Api-Key: <prefix>.<secret>` — per-caller DB-backed key with domain scopes, for external
  people. Authorises against THIS gateway only; never reaches the workers/Redis/DB directly.
  See [operations.md](operations.md#api-tokens) and `app/api_auth.py`.

## Databases (owned by core-api, except `portal`)
Per-DB declarative bases in `db-contract/Database/config.py`; logical names resolve to real DBs
by **substring** match against the `DB_NAME` env list. core-api is the only Alembic — run it
per-DB via `python tools/migrate.py <action> <db> …` (never bare `alembic`). The schema source of
truth is `db-contract/`; `app/Database/` and each worker's `worker/Database/` are vendored copies
(keep them in sync — `ServiceModels.py` is byte-identical in all four locations).

> PowerPlatform has been **removed** from the platform — routers, models, schemas, the
> `PowerPlatformBase`, external-worker's scheduler jobs, the powerplatform DB targeting and the
> `versionsPowerPlatform` migration tree. `MainModels` (Registrations/Airlines/Guests/Lease_Output)
> was kept.

## Redis keyspace (shared — avoid collisions)
- ARQ: `core:external`, `core:files`, `core:robot` (scraper robot) queues; `arq:job:*`, `arq:result:*`, `arq:in-progress:*`,
  `arq:retry:*`, health-check keys.
- Status: pub/sub channel `status:events`.
- file-processor queue: `fp:ready` (doorbell stream + group `fp-workers`), `fp:u:{group}` (per-group
  list), `fp:lease:{group}`, `fp:proc:{consumer}`, `fp:paused`.
- Scheduler/queue control: `queue:paused:{queue_name}`.
- App cache (core-api `DBProxy`/`cache_query`): `airline:*`, `aircraft:*`, `template:*`, … ;
  external-worker: `registration:*`, FlightRadar polling keys `flights:polling`, `flights:meta`,
  `fr:bootstrap_done`.
