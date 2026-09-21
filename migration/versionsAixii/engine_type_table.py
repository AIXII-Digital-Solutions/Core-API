"""Engines get their own type reference, like the airframes have

`fleet.aircraft_engine.master_series` was free text — the engine model written out on each
installation. It becomes `fleet.engine_type`, keyed the same way `fleet.aircraft_type` is: the PAIR
of manufacturer and master series, with both normalised into STORED generated columns and the
constraint declared NULLS NOT DISTINCT.

WHICH LEVEL OF NAME. Cirium nests engine names four deep — Engine Type (V2500), Engine Master
Series (V2500-A5), Engine Series (V2527), Engine Sub Series (V2527-A5); for CFM that reads CFM56,
CFM56-5, CFM56-5B, CFM56-5B3/3. This table holds the MASTER SERIES, which is the same granularity
`fleet.aircraft_type` holds for airframes, so the two sides of the domain describe hardware at one
level rather than two. Cirium has 365 distinct (manufacturer, master series) pairs, 464 at series
level and 952 at sub-series level — adding a finer column later is one migration, not a redesign.

Unlike the airframes, no engine master series in Cirium is built by more than one manufacturer. The
key is still the pair, because the reason it is a pair on the airframe side (licence production)
applies to engines too and the first collision should not need a migration to survive.

`fleet.aircraft_engine` loses `master_series` and gains `engine_type_id`. Nothing is migrated: the
table is empty, and the free-text column had never been written to.

Revision ID: engine_type_table
Revises: aircraft_type_manufacturer
Create Date: 2026-09-21
"""
from alembic import op

revision = "engine_type_table"
down_revision = "aircraft_type_manufacturer"
branch_labels = None
depends_on = None

_READ_ROLES = "grp_aixii_read, grp_aviation_write, svc_external_worker"
_WRITE_ROLE = "grp_api_write"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE fleet.engine_type (
            id                       bigserial PRIMARY KEY,
            manufacturer             text,
            manufacturer_normalized  text GENERATED ALWAYS AS (upper(btrim(manufacturer))) STORED,
            master_series            text NOT NULL,
            master_series_normalized text GENERATED ALWAYS AS (upper(btrim(master_series))) STORED,
            created_at               timestamp NOT NULL DEFAULT now(),
            updated_at               timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_engine_type_manufacturer_series
                UNIQUE NULLS NOT DISTINCT (manufacturer_normalized, master_series_normalized)
        )
    """)
    op.execute("CREATE INDEX ix_fleet_engine_type_manufacturer ON fleet.engine_type (manufacturer)")
    op.execute(
        "COMMENT ON TABLE fleet.engine_type IS "
        "'Engine models, keyed by manufacturer AND master series exactly as fleet.aircraft_type is. "
        "Holds Cirium''s Engine Master Series level (V2500-A5, CFM56-5), the same granularity the "
        "airframe side uses. Loaded by _admin/load_engine_types.py.'"
    )

    # the installation now points at the model instead of spelling it out
    op.execute("ALTER TABLE fleet.aircraft_engine DROP COLUMN IF EXISTS master_series")
    op.execute(
        "ALTER TABLE fleet.aircraft_engine ADD COLUMN engine_type_id bigint "
        "REFERENCES fleet.engine_type (id) ON DELETE RESTRICT"
    )
    op.execute(
        "CREATE INDEX ix_fleet_aircraft_engine_engine_type_id "
        "ON fleet.aircraft_engine (engine_type_id)"
    )

    op.execute(
        "CREATE TRIGGER engine_type_audit AFTER INSERT OR UPDATE OR DELETE ON fleet.engine_type "
        "FOR EACH ROW EXECUTE FUNCTION audit.log_change()"
    )

    op.execute(f"GRANT SELECT ON fleet.engine_type TO {_READ_ROLES}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON fleet.engine_type TO {_WRITE_ROLE}")
    op.execute(f"GRANT USAGE, SELECT ON SEQUENCE fleet.engine_type_id_seq TO {_WRITE_ROLE}")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS fleet.ix_fleet_aircraft_engine_engine_type_id")
    op.execute("ALTER TABLE fleet.aircraft_engine DROP COLUMN IF EXISTS engine_type_id")
    op.execute("ALTER TABLE fleet.aircraft_engine ADD COLUMN master_series text")
    op.execute("DROP TRIGGER IF EXISTS engine_type_audit ON fleet.engine_type")
    op.execute("DROP TABLE IF EXISTS fleet.engine_type")
