import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# The single source of truth for the schema is db-contract/Database. Its modules
# use bare imports (e.g. `from Database.config ...`), so put db-contract on the path.
import os
from urllib.parse import quote_plus

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "db-contract"))

# Migrations are infra (not a segment): they read the DB credentials from the
# repo-root .env / .env.dev. No dependency on any segment's Config.
_DEV = os.getenv("DEV_MODE", "false").lower() in ("1", "true", "yes", "on")
_env_file = _REPO_ROOT / (".env.dev" if _DEV else ".env")
if _env_file.exists():
    load_dotenv(_env_file)


def get_db_url(db_name: str) -> str:
    """DSN for the DB whose name contains ``db_name`` (matches DBSettings logic)."""
    user = os.getenv("DB_USER", "")
    password = os.getenv("DB_PASSWORD", "")
    host = os.getenv("DB_HOST", "localhost")
    port = os.getenv("DB_PORT", "5432")
    if not user or not password:
        raise ValueError("Database credentials not provided (DB_USER / DB_PASSWORD)")
    db_list = [d.strip() for d in os.getenv("DB_NAME", "").split(",") if d.strip()]
    matches = [d for d in db_list if db_name.lower() in d.lower()]
    if not matches:
        raise ValueError(f"No database similar to '{db_name}' found in {db_list}")
    if len(matches) > 1:
        raise ValueError(f"Ambiguous name '{db_name}', matches: {matches}")
    return (f"postgresql+asyncpg://{user}:{quote_plus(password)}@"
            f"{host}:{port}/{matches[0]}")

# Alembic Config
config = context.config

# Logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Which DB?
# NOTE: `-x db=...` selects ONLY target_metadata + the DB URL below. It does NOT
# switch the revision directory — Alembic builds the ScriptDirectory from alembic.ini
# BEFORE this env runs, so the `versions_dir` computed per-branch is informational only.
# To target a specific DB's revision tree you MUST either (a) activate the matching
# `version_locations = migration/versions<Db>` line in alembic.ini (and comment the
# others), or (b) pass `--version-path migration/versions<Db>` on the command line.
# Running a `revision` without doing so writes the new script into whatever single
# version_locations is active (currently versionsCirium) and chains onto its head.
db_name = context.get_x_argument(as_dictionary=True).get("db")

if db_name == "main":
    from Database.config import MainBase
    from Database.MainModels import *
    DATABASE_URL = get_db_url("main")
    target_metadata = MainBase.metadata
    versions_dir = "versionsMain"
    Base = MainBase

elif db_name == "service":
    from Database.config import ServiceBase
    from Database.ServiceModels import *
    DATABASE_URL = get_db_url("service")
    target_metadata = ServiceBase.metadata
    versions_dir = "versionsService"
    Base = ServiceBase

elif db_name == "cirium":
    from Database.config import CiriumBase
    from Database.CiriumModels import *
    DATABASE_URL = get_db_url("cirium")
    target_metadata = CiriumBase.metadata
    versions_dir = "versionsCirium"
    Base = CiriumBase

elif db_name == "airlabs":
    from Database.config import AirlabsBase
    from Database.AirlabsModels import *
    DATABASE_URL = get_db_url("airlabs")
    target_metadata = AirlabsBase.metadata
    versions_dir = "versionsAirlabs"
    Base = AirlabsBase

elif db_name in {"fr", "flightradar"}:
    from Database.config import FlightRadarBase
    from Database.FlightRadarModels import *
    DATABASE_URL = get_db_url("flightradar")
    target_metadata = FlightRadarBase.metadata
    versions_dir = "versionsFlightRadar"
    Base = FlightRadarBase

elif db_name in {"pp", "power", "powerplatform"}:
    from Database.config import PowerPlatformBase
    from Database.PowerPlatformModels import *
    DATABASE_URL = get_db_url("powerplatform")
    target_metadata = PowerPlatformBase.metadata
    versions_dir = "versionsPowerPlatform"
    Base = PowerPlatformBase

elif db_name in {"aviationedge", "ae"}:
    from Database.config import AviationEdgeBase
    from Database.AviationEdgeModels import *
    DATABASE_URL = get_db_url("aviationedge")
    target_metadata = AviationEdgeBase.metadata
    versions_dir = "versionsAviationEdge"
    Base = AviationEdgeBase

else:
    raise ValueError("Unknown DB. Use: alembic -x db=main|service|cirium|airlabs|flightradar|powerplatform|aviationedge ...")


# Set runtime DB URL
config.set_main_option("sqlalchemy.url", DATABASE_URL)


# ---------------------------------------------------------------------------
# FILTER: exclude tables not present in SQLAlchemy models
# ---------------------------------------------------------------------------
def include_object(object, name, type_, reflected, compare_to):
    if type_ == "table":
        if name in target_metadata.tables:
            print("Include:", name)
            return True
        else:
            print("Skip   :", name)
            return False
    return True


# ---------------------------------------------------------------------------
# Config shared by both offline and online migration modes
# ---------------------------------------------------------------------------
def get_alembic_config_kwargs():
    return dict(
        target_metadata=target_metadata,
        compare_type=True,
        include_object=include_object,
    )


# ---------------------------------------------------------------------------
# Offline migrations
# ---------------------------------------------------------------------------
def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")

    context.configure(
        url=url,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **get_alembic_config_kwargs(),
    )

    with context.begin_transaction():
        context.run_migrations()


# ---------------------------------------------------------------------------
# Online migrations
# ---------------------------------------------------------------------------
def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        **get_alembic_config_kwargs(),
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


# Debug output: list model tables discovered by SQLAlchemy
print("Models detected in metadata:", list(Base.metadata.tables.keys()))

# Run Alembic
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
