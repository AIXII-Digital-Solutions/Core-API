"""service.forecast_profiles — named, portal-editable forecast model parameters.

The forecast model's tuning knobs (level window, seasonal shrinkage, frontier thresholds, horizon, …) were
hardcoded constants in external-worker's ForecastAPI/model.py, so retuning meant a code change and a
redeploy. This table moves them to runtime: the portal edits a named profile via core-api's
/forecast/profiles router, and external-worker reads the profile at the start of each panel run.

`params` is JSONB holding OVERRIDES ONLY — absent key means "use the default from forecast_params.SPEC".
Deliberately not a column-per-parameter: the knobs are specific to THIS model version (a future forecast
model has a different set), so columns would mean a migration per knob plus a table half-full of NULLs for
models that do not use them; and the portal renders its form from the served spec, which a JSONB + spec pair
supports without a portal redeploy when a knob is added. `model_version` guards the reinterpretation risk
that JSONB otherwise carries — the resolver refuses a row written against a parameter set it does not know.

Seeds one row, name='default', with an EMPTY override set. Empty is the point: the row defers entirely to
forecast_params.SPEC, so this migration adds a control surface without itself deciding anything. (The
forecast's behaviour DOES change in the same release, but from an unrelated decision: level_window's default
moved 9 -> 15 after a backtest — see that knob's SPEC entry. This table neither caused nor records that.)

The partial unique index allows at most one is_default row.

Revision ID: forecast_profiles
Revises: forecast_step_timings
Create Date: 2026-07-17
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "forecast_profiles"
down_revision: Union[str, Sequence[str], None] = "forecast_step_timings"
branch_labels = None
depends_on = None

# keep in sync with forecast_params.MODEL_VERSION
_MODEL_VERSION = "acys-v1"


def upgrade() -> None:
    op.create_table(
        "forecast_profiles",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("model_version", sa.String(length=32), nullable=False),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("updated_by", sa.String(length=128), nullable=True),
        sa.UniqueConstraint("name", name="uq_forecast_profiles_name"),
    )
    op.create_index("ix_forecast_profiles_name", "forecast_profiles", ["name"], unique=True)
    # at most ONE default profile — enforced by the database, not by the router's good intentions
    op.create_index("ix_forecast_profiles_default", "forecast_profiles", ["is_default"],
                    unique=True, postgresql_where=sa.text("is_default"))

    # The seed's params are EMPTY on purpose: "{}" resolves to exactly the shipped defaults, so a cluster
    # that migrates and does nothing else forecasts precisely as it did before.
    op.execute(sa.text(
        "INSERT INTO forecast_profiles (name, description, model_version, params, is_default, enabled) "
        "VALUES (:n, :d, :v, '{}'::jsonb, true, true)"
    ).bindparams(
        n="default",
        d="Профиль по умолчанию: параметры модели как в коде. Пустой набор переопределений.",
        v=_MODEL_VERSION,
    ))

    # No GRANTs here on purpose: migrations run as `developer`, and db-aixii-setup.sql's
    # ALTER DEFAULT PRIVILEGES FOR ROLE developer already hands grp_service_write (which svc_external_worker
    # and svc_api are members of) SELECT/INSERT/UPDATE/DELETE on new public tables and USAGE on new
    # sequences. Same reason forecast_step_timings needed none. Verified against the live cluster.


def downgrade() -> None:
    op.drop_index("ix_forecast_profiles_default", table_name="forecast_profiles")
    op.drop_index("ix_forecast_profiles_name", table_name="forecast_profiles")
    op.drop_table("forecast_profiles")
