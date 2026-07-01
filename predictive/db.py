"""Standalone DB helper for the forecasting calibration package.

Reads credentials straight from the repo-root ``.env`` (no secrets in code) and
talks to the ``aixii`` Postgres via the already-installed ``asyncpg`` driver —
mirrors ``app/Config/config.py``'s ``DBSettings.get_db_url`` but without dragging
in the FastAPI app's bare-import layout. Read-only by default; the diagnose
entrypoint uses ``execute``/``execute_file`` to (re)build the ``api.af_*`` matviews.
"""
from __future__ import annotations

import os
from pathlib import Path

import asyncpg
import pandas as pd
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
SQL_DIR = Path(__file__).resolve().parent / "archetype" / "sql"


def conn_params(database: str | None = None) -> dict:
    """Build asyncpg connection kwargs from ``.env``.

    ``database`` overrides the target DB; default is ``DB_AIXII_NAME`` (prod
    ``aixii`` holds the ~8M FR24 panel). Credentials are passed as keyword args,
    so the special characters in the password need no URL-encoding.
    """
    cfg = dotenv_values(ENV_PATH)
    if not cfg.get("DB_USER") or not cfg.get("DB_PASSWORD"):
        raise RuntimeError(f"DB_USER/DB_PASSWORD missing in {ENV_PATH}")
    return dict(
        user=cfg["DB_USER"],
        password=cfg["DB_PASSWORD"],
        host=cfg["DB_HOST"],
        port=int(cfg["DB_PORT"]),
        database=database or cfg.get("DB_AIXII_NAME") or "aixii",
    )


class DB:
    """A single long-lived asyncpg connection with small helpers."""

    def __init__(self, database: str | None = None, statement_timeout_ms: int = 0):
        self._params = conn_params(database)
        self._statement_timeout_ms = statement_timeout_ms
        self._conn: asyncpg.Connection | None = None

    async def __aenter__(self) -> "DB":
        self._conn = await asyncpg.connect(**self._params, timeout=60)
        # 0 = no timeout: the af_base matview joins ~7M rows and can run long.
        await self._conn.execute(f"SET statement_timeout = {int(self._statement_timeout_ms)}")
        return self

    async def __aexit__(self, *exc) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> asyncpg.Connection:
        if self._conn is None:
            raise RuntimeError("DB not connected; use `async with DB() as db:`")
        return self._conn

    async def execute(self, sql: str) -> str:
        return await self.conn.execute(sql)

    async def execute_file(self, name_or_path: str, **subs: str) -> str:
        """Run a .sql file. ``subs`` does literal ``{key}`` placeholder replacement
        (we avoid str.format so SQL braces/`$$` never collide)."""
        path = Path(name_or_path)
        if not path.is_absolute():
            path = SQL_DIR / name_or_path
        sql = path.read_text(encoding="utf-8")
        for k, v in subs.items():
            sql = sql.replace("{" + k + "}", v)
        return await self.conn.execute(sql)

    async def fetch(self, sql: str, *args) -> list[asyncpg.Record]:
        return await self.conn.fetch(sql, *args)

    async def fetch_val(self, sql: str, *args):
        return await self.conn.fetchval(sql, *args)

    async def fetch_df(self, sql: str, *args) -> pd.DataFrame:
        rows = await self.conn.fetch(sql, *args)
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame([dict(r) for r in rows], columns=list(rows[0].keys()))
