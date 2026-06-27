# core-api

FastAPI gateway for the AIXII platform **and** the owner of the database schema for
**all databases except `portal`**. Accepts internet traffic, returns data, dispatches
background jobs, forwards uploaded files to file-processor, and exposes job status
(REST + SSE). Holds the source of truth for the core DB schema (`db-contract/`) and the
only Alembic in the platform (per-DB via `tools/migrate.py`).

## Layout
```
app/                 # FastAPI service (Config, Database, Schemas, Utils, Queue.py, Routers, …)
db-contract/         # SCHEMA SOURCE OF TRUTH (all DBs except portal) — edit here
  Database/          #   core ORM models + Bases
  Schemas/           #   needed by models (Schemas.Enums) for autogenerate
migration/, alembic.ini   # Alembic; run via tools/migrate.py
tools/migrate.py     # per-DB migration runner (sets version_locations correctly)
Dockerfile, docker-compose.yml, entrypoint.sh, .env.example
infra/docker-compose.yml  # OPTIONAL self-hosted shared Postgres+Redis
```
`app/Database` is core-api's own copy of the models (no sync — the workers vendor their
own subsets; the portal owns its own schema).

## Run (Docker)
```bash
cp .env.example .env           # DB_*/REDIS_*, SERVICE_TOKEN, FILE_PROCESSOR_URL/TOKEN
docker compose up -d --build
# health: GET http://localhost:8000/health/
```
Point `DB_HOST` / `REDIS_HOST` at the SHARED cluster. Need one locally?
`DB_USER=… DB_PASSWORD=… docker compose -f infra/docker-compose.yml up -d`.

## Schema changes (this service owns them — no sync)
1. Edit models in `db-contract/Database`.
2. Generate + apply a migration with the per-DB runner (do NOT use bare `alembic -x db=...`,
   which applies the wrong revision tree):
   ```
   python tools/migrate.py revision main "add X"   # --autogenerate
   python tools/migrate.py upgrade  main head
   ```
   DBs: `main service cirium airlabs flightradar aviationedge` (portal is owned by the portal service).
3. If a worker uses a changed table, copy the updated model file into that worker's repo and commit.

Migrations are a deliberate, pre-deploy step (set `RUN_MIGRATIONS=true` + `MIGRATE_DBS="…"`
only if you want the container to migrate on start).

## Talks to
- **PostgreSQL** (shared cluster) — owns all core databases (not portal).
- **Redis / ARQ** — enqueues jobs onto `core:external` for external-worker; reads `status:events` for SSE.
- **file-processor** (outbound HTTP) — saves an upload then POSTs it to `FILE_PROCESSOR_URL/process`.
- **Portal** (inbound HTTP) — server-to-server via `X-Service-Token` (`SERVICE_TOKEN`).
