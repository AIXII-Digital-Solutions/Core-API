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
    """DSN for the PHYSICAL database behind a logical name (mirrors DBSettings.physical_db):
    everything except `service` -> the `aixii` database."""
    user = os.getenv("DB_USER", "")
    password = os.getenv("DB_PASSWORD", "")
    host = os.getenv("DB_HOST", "localhost")
    port = os.getenv("DB_PORT", "5432")
    if not user or not password:
        raise ValueError("Database credentials not provided (DB_USER / DB_PASSWORD)")
    if str(db_name).strip().lower() == "service":
        real = os.getenv("DB_SERVICE_NAME", "service")
    else:
        real = os.getenv("DB_AIXII_NAME", "aixii")
    return (f"postgresql+asyncpg://{user}:{quote_plus(password)}@"
            f"{host}:{port}/{real}")

# Alembic Config
config = context.config

# Logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Which DB?
# NOTE: `-x db=...` selects ONLY target_metadata + the DB URL below. It does NOT switch the
# revision directory — Alembic builds the ScriptDirectory from alembic.ini BEFORE this env runs.
# Use tools/migrate.py (it sets version_locations per `-x db=` first); never bare `alembic`.
#
# After the AIXII consolidation there are exactly TWO targets:
#   aixii   — every aviation domain as a SCHEMA in one DB (combined metadata + include_schemas)
#   service — the separate service DB (job_statuses / schedule_registry / api_tokens)
# `main`/core is being rewritten and is intentionally NOT migrated yet.
db_name = context.get_x_argument(as_dictionary=True).get("db")

if db_name == "aixii":
    from Database.config import (CiriumBase, AirlabsBase, FlightRadarBase, AviationEdgeBase,
                                 ApiBase, IcaoBase)
    from Database.CiriumModels import *        # noqa: F401,F403  (register tables on the Bases)
    from Database.AirlabsModels import *       # noqa: F401,F403
    from Database.FlightRadarModels import *   # noqa: F401,F403
    from Database.AviationEdgeModels import *  # noqa: F401,F403
    from Database.ApiModels import *           # noqa: F401,F403
    from Database.IcaoModels import *          # noqa: F401,F403
    DATABASE_URL = get_db_url("aixii")
    # a list of MetaData (one per aviation schema) — alembic autogenerate compares all of them
    target_metadata = [
        CiriumBase.metadata,
        AirlabsBase.metadata,
        FlightRadarBase.metadata,
        AviationEdgeBase.metadata,
        ApiBase.metadata,
        IcaoBase.metadata,
    ]
    versions_dir = "versionsAixii"
    include_schemas = True

elif db_name == "service":
    from Database.config import ServiceBase
    from Database.ServiceModels import *       # noqa: F401,F403
    DATABASE_URL = get_db_url("service")
    target_metadata = ServiceBase.metadata
    versions_dir = "versionsService"
    include_schemas = False

else:
    raise ValueError("Unknown DB. Use: alembic -x db=aixii|service ...")


# Set runtime DB URL.
# The password is URL-encoded (quote_plus), so the DSN can contain `%XX` escapes. Alembic's
# Config is a ConfigParser with BasicInterpolation, which treats a lone `%` as interpolation
# syntax and raises "invalid interpolation syntax". Double the `%` here so set_main_option's
# before_set validation passes; ConfigParser collapses `%%`->`%` again when env_from_config
# reads the section back, and SQLAlchemy then URL-decodes `%XX` to the real password.
config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))


def _all_metadata():
    return target_metadata if isinstance(target_metadata, (list, tuple)) else [target_metadata]


# ---------------------------------------------------------------------------
# FILTER: include only tables present in our models (across all schemas).
# Under include_schemas reflected objects arrive as bare `name` + a separate `schema`,
# while metadata keys are `schema.table` — so compare on (schema, name).
# ---------------------------------------------------------------------------
def include_object(object, name, type_, reflected, compare_to):
    if type_ == "table":
        schema = getattr(object, "schema", None)
        key = f"{schema}.{name}" if schema else name
        for m in _all_metadata():
            if key in m.tables or name in m.tables:
                return True
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
        include_schemas=include_schemas,
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
print("Models detected in metadata:", [t for m in _all_metadata() for t in m.tables.keys()])

# Run Alembic
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
