"""Bring the hand-written tables back in line with their models

`aircraft_type_manufacturer`, `engine_type_table` and `service_info_table` created their columns
with raw SQL, and raw SQL does not have to agree with the model it is supposed to implement. It
did not, in four ways, all of which `alembic check` reported:

  * `text` where the models declare `String` — which PostgreSQL renders as `varchar`. Behaviourally
    identical, and autogenerate flags it forever. Worse, `fleet.aircraft_type` ended up holding its
    two generated columns as different types, `master_series_normalized` varchar (created from the
    model) and `manufacturer_normalized` text (added by hand).
  * `fleet.engine_type.master_series_normalized` was nullable although the model says it is not.
    A generated column over a NOT NULL source is never actually null, so this cost nothing today
    and would have cost an afternoon the first time somebody trusted the model.
  * `ix_service_info_source` / `ix_service_info_status` were named by hand, while `index=True` on
    the model implies `ix_fleet_service_info_*`. Every future autogenerate would have proposed
    dropping one pair and creating the other.
  * comments existed in the database and not on the models, so autogenerate kept proposing to
    remove them. The models now declare them, which is the half that was missing — the comment is
    documentation the database carries, and it belongs in the file people read.

The one drift left afterwards is on `api.registration`, which predates all of this; its model now
declares the `Text` and the comments the table already has, so a clean `alembic check` finally
means something.

Revision ID: align_type_tables
Revises: service_info_table
Create Date: 2026-09-21
"""
from alembic import op

revision = "align_type_tables"
down_revision = "service_info_table"
branch_labels = None
depends_on = None

# Plain columns the model declares as String and the hand-written DDL made text. A generated
# column's OWN type can be altered in place; a column it READS cannot, which is why engine_type
# needs the longer dance below.
_TO_VARCHAR = (
    ("fleet", "aircraft_type", "manufacturer_normalized"),   # generated, but nothing reads it
    ("fleet", "service_info", "usage_status"),
)

# fleet.engine_type: both base columns are read by a generated column, and both generated columns
# are read by the unique constraint. PostgreSQL refuses `ALTER COLUMN ... TYPE` on a column a
# generated column depends on, so the dependants come off, the base types change, and they go back.
_ENGINE_TYPE_REBUILD = (
    "ALTER TABLE fleet.engine_type DROP CONSTRAINT uq_engine_type_manufacturer_series",
    "ALTER TABLE fleet.engine_type DROP COLUMN manufacturer_normalized",
    "ALTER TABLE fleet.engine_type DROP COLUMN master_series_normalized",
    "ALTER TABLE fleet.engine_type ALTER COLUMN manufacturer TYPE varchar",
    "ALTER TABLE fleet.engine_type ALTER COLUMN master_series TYPE varchar",
    "ALTER TABLE fleet.engine_type ADD COLUMN manufacturer_normalized varchar "
    "GENERATED ALWAYS AS (upper(btrim(manufacturer))) STORED",
    "ALTER TABLE fleet.engine_type ADD COLUMN master_series_normalized varchar "
    "GENERATED ALWAYS AS (upper(btrim(master_series))) STORED",
    "ALTER TABLE fleet.engine_type ALTER COLUMN master_series_normalized SET NOT NULL",
    "ALTER TABLE fleet.engine_type ADD CONSTRAINT uq_engine_type_manufacturer_series "
    "UNIQUE NULLS NOT DISTINCT (manufacturer_normalized, master_series_normalized)",
)

_INDEX_RENAMES = (
    ("ix_service_info_source", "ix_fleet_service_info_source"),
    ("ix_service_info_status", "ix_fleet_service_info_status"),
)


def upgrade() -> None:
    for schema, table, column in _TO_VARCHAR:
        op.execute(f"ALTER TABLE {schema}.{table} ALTER COLUMN {column} TYPE varchar")
    for statement in _ENGINE_TYPE_REBUILD:
        op.execute(statement)
    for old, new in _INDEX_RENAMES:
        op.execute(f"ALTER INDEX IF EXISTS fleet.{old} RENAME TO {new}")

    # the loader was merged into one script for both catalogues; the comment still named the old one
    op.execute(
        "COMMENT ON TABLE fleet.engine_type IS "
        "'Engine models, keyed by manufacturer AND master series exactly as fleet.aircraft_type "
        "is. Holds Cirium''s Engine Master Series level (V2500-A5, CFM56-5), the same granularity "
        "the airframe side uses. Loaded by _admin/load_types.py.'"
    )
    # lease_currency carried the warning about holding a contract''s currency per aircraft and
    # policy_currency did not, which read as though only one of them had the problem
    op.execute(
        "COMMENT ON COLUMN fleet.service_info.policy_currency IS "
        "'The policy''s currency. Held per aircraft since revision service_info_table, so nothing "
        "stops two aircraft on one policy disagreeing - whoever writes them must keep them "
        "consistent.'"
    )


def downgrade() -> None:
    for old, new in _INDEX_RENAMES:
        op.execute(f"ALTER INDEX IF EXISTS fleet.{new} RENAME TO {old}")
    for statement in (
        "ALTER TABLE fleet.engine_type DROP CONSTRAINT uq_engine_type_manufacturer_series",
        "ALTER TABLE fleet.engine_type DROP COLUMN manufacturer_normalized",
        "ALTER TABLE fleet.engine_type DROP COLUMN master_series_normalized",
        "ALTER TABLE fleet.engine_type ALTER COLUMN manufacturer TYPE text",
        "ALTER TABLE fleet.engine_type ALTER COLUMN master_series TYPE text",
        "ALTER TABLE fleet.engine_type ADD COLUMN manufacturer_normalized text "
        "GENERATED ALWAYS AS (upper(btrim(manufacturer))) STORED",
        "ALTER TABLE fleet.engine_type ADD COLUMN master_series_normalized text "
        "GENERATED ALWAYS AS (upper(btrim(master_series))) STORED",
        "ALTER TABLE fleet.engine_type ADD CONSTRAINT uq_engine_type_manufacturer_series "
        "UNIQUE NULLS NOT DISTINCT (manufacturer_normalized, master_series_normalized)",
    ):
        op.execute(statement)
    for schema, table, column in _TO_VARCHAR:
        op.execute(f"ALTER TABLE {schema}.{table} ALTER COLUMN {column} TYPE text")
    op.execute("COMMENT ON COLUMN fleet.service_info.policy_currency IS NULL")
