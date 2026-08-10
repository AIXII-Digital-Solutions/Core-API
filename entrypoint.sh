#!/usr/bin/env sh
# core-api entrypoint: wait for the DB, optionally run migrations, then start the API.
set -e

echo "[entrypoint] waiting for database ${DB_HOST:-localhost}:${DB_PORT:-5432} ..."
python - <<'PY'
import os, socket, time, sys
host = os.getenv("DB_HOST", "localhost"); port = int(os.getenv("DB_PORT", "5432"))
for _ in range(60):
    try:
        socket.create_connection((host, port), 2).close()
        print("[entrypoint] database reachable"); sys.exit(0)
    except OSError:
        time.sleep(2)
print("[entrypoint] WARNING: database not reachable after timeout"); sys.exit(0)
PY

# With a vault backend, resolve the boot-critical secrets BEFORE starting the app so a bad vault
# produces one classified line at the top of the log ("Vaultwarden unreachable", "item not found")
# instead of a Python traceback out of an import. Deliberately NOT the three provider API keys:
# core-api never calls those providers, so a missing one must not block a boot.
# Costs one extra unlock+sync (seconds); set CHECK_SECRETS_ON_BOOT=false to skip.
if [ "${SECRETS_BACKEND:-env}" != "env" ] && [ "${CHECK_SECRETS_ON_BOOT:-true}" = "true" ]; then
  echo "[entrypoint] resolving boot secrets from ${SECRETS_BACKEND} ..."
  if ! python tools/check_secrets.py \
        DB_USER DB_PASSWORD REDIS_USER REDIS_USER_PASSWORD SERVICE_TOKEN FILE_PROCESSOR_TOKEN; then
    echo "[entrypoint] FATAL: could not resolve the boot secrets — refusing to start." >&2
    exit 1
  fi
fi

# Migrations are a deliberate operation (multi-DB; alembic.ini selects ONE version_locations
# at a time). Run them as a separate job/step, not implicitly on every boot. Set
# RUN_MIGRATIONS=true only if you intentionally want this container to migrate on start.
if [ "${RUN_MIGRATIONS:-false}" = "true" ]; then
  echo "[entrypoint] RUN_MIGRATIONS=true — applying migrations for: ${MIGRATE_DBS:-aixii service}"
  for db in ${MIGRATE_DBS:-aixii service}; do
    echo "[entrypoint]   python tools/migrate.py upgrade $db head"
    python tools/migrate.py upgrade "$db" head || echo "[entrypoint]   (migration for $db failed/skipped)"
  done
fi

echo "[entrypoint] starting core-api on :${PORT:-8000}"
exec python app/main.py
