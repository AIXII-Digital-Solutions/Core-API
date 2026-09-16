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

# OPTIONAL pre-flight: resolve the boot-critical secrets in their own process before the app starts.
# OFF by default, because it is a whole extra vault round trip (unlock, sync, read) and the app now
# reports the same failure itself, as one classified line rather than a traceback — see app/main.py.
# Turn it on (CHECK_SECRETS_ON_BOOT=true) while setting a host up, when knowing WHICH key is wrong
# before anything else starts is worth the seconds. Deliberately not the provider API keys (nothing
# in core-api resolves them) nor PBIE_CLIENT_SECRET (it gates /capacity, which answers 503 without it).
if [ "${SECRETS_BACKEND:-env}" != "env" ] && [ "${CHECK_SECRETS_ON_BOOT:-false}" = "true" ]; then
  echo "[entrypoint] resolving boot secrets from ${SECRETS_BACKEND} ..."
  if ! python tools/check_secrets.py \
        DB_USER DB_PASSWORD REDIS_USER REDIS_USER_PASSWORD SERVICE_TOKEN FILE_PROCESSOR_TOKEN \
        MS_WEBHOOK_SECRET; then
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
