"""main.airports_ref -> main.airports + indexes (icao/iata/region/country/greater_region/type +
composites) + per-usage views

Revision ID: airports_rename_views
Revises: airports_ref_all
Create Date: 2026-07-02
"""
from alembic import op

revision = "airports_rename_views"
down_revision = "airports_ref_all"
branch_labels = None
depends_on = None

_NEW_INDEXES = [
    ("ix_airports_icao", "(icao)"),
    ("ix_airports_region", "(region)"),
    ("ix_airports_country", "(country)"),
    ("ix_airports_greater_region", "(greater_region)"),
    ("ix_airports_type", "(type)"),
    ("ix_airports_type_region", "(type, region)"),
    ("ix_airports_type_country", "(type, country)"),
    ("ix_airports_type_greater_region", "(type, greater_region)"),
]

# view name -> type filter
_VIEWS = {
    "airports_for_airplanes": "type IN ('large_airport','medium_airport','small_airport')",
    "airports_for_helicopters": "type = 'heliport'",
    "airports_for_seaplanes": "type = 'seaplane_base'",
    "airports_for_balloons": "type = 'balloonport'",
}

_VIEW_GRANTS = r"""
DO $do$ DECLARE v text; BEGIN
    FOREACH v IN ARRAY ARRAY['airports_for_airplanes','airports_for_helicopters','airports_for_seaplanes','airports_for_balloons'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='grp_aviation_write') THEN
            EXECUTE format('GRANT SELECT ON main.%I TO grp_aviation_write', v); END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='grp_aixii_read') THEN
            EXECUTE format('GRANT SELECT ON main.%I TO grp_aixii_read', v); END IF;
    END LOOP;
END $do$;
"""


def upgrade() -> None:
    op.execute("ALTER TABLE main.airports_ref RENAME TO airports")
    op.execute("ALTER INDEX IF EXISTS main.ix_airports_ref_iata RENAME TO ix_airports_iata")
    op.execute("ALTER INDEX IF EXISTS main.ix_airports_ref_ident RENAME TO ix_airports_ident")
    op.execute("ALTER INDEX IF EXISTS main.ix_airports_ref_country_code RENAME TO ix_airports_country_code")
    op.execute("ALTER SEQUENCE IF EXISTS main.airports_ref_id_seq RENAME TO airports_id_seq")

    for name, cols in _NEW_INDEXES:
        op.execute(f"CREATE INDEX {name} ON main.airports {cols}")

    for name, cond in _VIEWS.items():
        op.execute(f"CREATE VIEW main.{name} AS SELECT * FROM main.airports WHERE {cond}")
    op.execute(_VIEW_GRANTS)


def downgrade() -> None:
    for name in _VIEWS:
        op.execute(f"DROP VIEW IF EXISTS main.{name}")
    for name, _cols in _NEW_INDEXES:
        op.execute(f"DROP INDEX IF EXISTS main.{name}")
    op.execute("ALTER SEQUENCE IF EXISTS main.airports_id_seq RENAME TO airports_ref_id_seq")
    op.execute("ALTER INDEX IF EXISTS main.ix_airports_country_code RENAME TO ix_airports_ref_country_code")
    op.execute("ALTER INDEX IF EXISTS main.ix_airports_ident RENAME TO ix_airports_ref_ident")
    op.execute("ALTER INDEX IF EXISTS main.ix_airports_iata RENAME TO ix_airports_ref_iata")
    op.execute("ALTER TABLE main.airports RENAME TO airports_ref")
