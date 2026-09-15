import os
import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Any

from sqlalchemy import event, exc
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine, AsyncSession
from sqlalchemy.util import await_only

# Pool sizing is PER PROCESS. The API runs several uvicorn workers, each with its own engine, and they
# all share one PostgreSQL max_connections with every other service on the cluster — so the per-worker
# numbers are small on purpose. Tune through the environment rather than here.
_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "3"))
_MAX_OVERFLOW = int(os.getenv("DB_MAX_OVERFLOW", "5"))
_POOL_RECYCLE_S = int(os.getenv("DB_POOL_RECYCLE_SECONDS", "1800"))
# A pooled connection idle for longer than this is checked before it is handed out; a busier one is not.
_PING_IDLE_S = float(os.getenv("DB_PING_IDLE_SECONDS", "30"))


def _install_idle_ping(engine: AsyncEngine) -> None:
    """Validate a pooled connection on checkout ONLY if it has been idle, in ONE round trip.

    The stock `pool_pre_ping=True` pinged on EVERY checkout, and with the asyncpg adapter the ping is
    wrapped in its own transaction — measured on the wire as `BEGIN; ; ROLLBACK;`, three round trips
    before the request's first real statement. Between this API and the database each round trip costs
    tens of milliseconds, so that was the single most expensive thing a typical GET did.

    A connection returned to the pool moments ago is not going to be stale, so only one idle past
    DB_PING_IDLE_SECONDS is checked, and it is checked with a bare `SELECT 1` on the raw asyncpg
    connection: no transaction, one round trip. A failed check raises DisconnectionError, which makes
    the pool discard that connection and hand out a fresh one — the request never sees the dead one.
    The window this leaves (a connection that died less than DB_PING_IDLE_SECONDS after its last use,
    e.g. a database restart mid-traffic) fails one request per such connection and self-heals."""

    @event.listens_for(engine.sync_engine, "checkin")
    def _stamp_checkin(dbapi_connection, connection_record):
        connection_record.info["last_checkin"] = time.monotonic()

    @event.listens_for(engine.sync_engine, "checkout")
    def _ping_if_idle(dbapi_connection, connection_record, connection_proxy):
        last = connection_record.info.get("last_checkin")
        if last is None or time.monotonic() - last < _PING_IDLE_S:
            return            # freshly opened, or used moments ago
        try:
            # Runs inside the greenlet the async engine checks connections out in, so await_only is
            # legal here. The raw asyncpg connection, not the SQLAlchemy adapter: the adapter would
            # open a transaction around the ping.
            await_only(dbapi_connection._connection.execute("SELECT 1"))
        except Exception as e:
            raise exc.DisconnectionError(f"pooled connection failed its idle check: {e}") from e


class DatabaseClient:
    """Async session factory keyed by PHYSICAL database.

    After the AIXII consolidation there are TWO physical databases: `aixii` (every aviation
    domain as a schema) and `service`. The public API is unchanged — callers still pass the
    logical name (`main`/`cirium`/`airlabs`/`flightradar`/`aviationedge`/`service`); DBSettings
    maps it to a physical DB (``physical_db``) and the engine/session cache is keyed by that
    PHYSICAL name, so the five aviation logical names SHARE one pooled engine. Which table a
    query hits is decided by the model's schema (see Database/config.py), not by the session.

    Two kinds of session:
      * ``session()``       — transactional: BEGIN ... COMMIT (or ROLLBACK on error). For anything
                              that writes, or that needs transaction-local state (`SET LOCAL`,
                              `set_config(..., true)` as the insurance audit trail uses).
      * ``read_session()``  — AUTOCOMMIT: no BEGIN, no COMMIT, just the statements. For read-only
                              work. Under READ COMMITTED every statement already takes its own
                              snapshot even inside a transaction, so dropping the transaction changes
                              nothing a reader can observe — it only removes two round trips.
    """

    def __init__(self):
        # DBSettings is resolved lazily from the host service's own Config so that importing the
        # Database package never requires a Config to be present (e.g. for Alembic).
        from Config import DBSettings
        self.settings = DBSettings()
        self._engines: dict[str, AsyncEngine] = {}            # keyed by physical DB
        self._session_factories: dict[str, async_sessionmaker] = {}
        self._read_session_factories: dict[str, async_sessionmaker] = {}

    def _get_engine(self, db_name: str) -> AsyncEngine:
        """Return (creating on first use) the engine for the PHYSICAL DB behind ``db_name``."""
        phys = self.settings.physical_db(db_name)
        if phys not in self._engines:
            engine = create_async_engine(
                self.settings.get_db_url(db_name),
                echo=False,
                pool_size=_POOL_SIZE,
                max_overflow=_MAX_OVERFLOW,
                pool_recycle=_POOL_RECYCLE_S,
                pool_pre_ping=False,          # replaced by _install_idle_ping — see there
                future=True,
            )
            _install_idle_ping(engine)
            self._engines[phys] = engine
            self._session_factories[phys] = async_sessionmaker(
                engine, class_=AsyncSession, expire_on_commit=False
            )
            # Same pool, AUTOCOMMIT isolation: execution_options returns a proxy sharing the engine.
            self._read_session_factories[phys] = async_sessionmaker(
                engine.execution_options(isolation_level="AUTOCOMMIT"),
                class_=AsyncSession, expire_on_commit=False,
            )
        return self._engines[phys]

    @asynccontextmanager
    async def session(self, db_name: str) -> AsyncGenerator[AsyncSession | Any, Any]:
        """Context-managed session for the physical DB behind ``db_name`` (auto commit/rollback)."""
        phys = self.settings.physical_db(db_name)
        if phys not in self._session_factories:
            self._get_engine(db_name)

        session_factory = self._session_factories[phys]

        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    @asynccontextmanager
    async def read_session(self, db_name: str) -> AsyncGenerator[AsyncSession | Any, Any]:
        """READ-ONLY session for the physical DB behind ``db_name``: autocommit, no transaction.

        Do not write through it. A write would still land — each statement commits on its own — but
        without atomicity, and transaction-local settings (`SET LOCAL`, `set_config(..., true)`)
        evaporate the moment their statement ends."""
        phys = self.settings.physical_db(db_name)
        if phys not in self._read_session_factories:
            self._get_engine(db_name)

        async with self._read_session_factories[phys]() as session:
            yield session

    async def dispose(self):
        for engine in self._engines.values():
            await engine.dispose()


__all__ = ['DatabaseClient']
