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

# Migrations are a deliberate operation (multi-DB; alembic.ini selects ONE version_locations
# at a time). Run them as a separate job/step, not implicitly on every boot. Set
# RUN_MIGRATIONS=true only if you intentionally want this container to migrate on start.
if [ "${RUN_MIGRATIONS:-false}" = "true" ]; then
  echo "[entrypoint] RUN_MIGRATIONS=true — applying migrations for: ${MIGRATE_DBS:-main service}"
  for db in ${MIGRATE_DBS:-main service}; do
    echo "[entrypoint]   python tools/migrate.py upgrade $db head"
    python tools/migrate.py upgrade "$db" head || echo "[entrypoint]   (migration for $db failed/skipped)"
  done
fi

echo "[entrypoint] starting core-api on :${PORT:-8000}"
exec python app/main.py
