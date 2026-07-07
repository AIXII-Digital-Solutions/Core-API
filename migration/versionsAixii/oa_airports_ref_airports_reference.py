"""main.airports_ref — standalone airports reference (loaded from OurAirports, independent of
main.virtual_airport_list / flightradar.airports)

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-02

A clean, comprehensive airport reference keyed by IATA, sourced ONLY from OurAirports open data
(airports.csv + countries.csv + runways.csv), loaded by _admin/load_ourairports.py. Fills the City
gap that virtual_airport_list (~97% NULL City) and flightradar.airports (1019 airports) cannot.

Columns: iata, icao, name, city, country (+ country_code), longitude, latitude, type
(large/medium/small_airport/heliport/...), elevation_ft, region (ISO 3166-2, e.g. US-CA),
greater_region (continent name), continent_code, runways (jsonb array), ourairports_id.
"""
from alembic import op

revision = "oa_airports_ref"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None

TABLE_SQL = """
CREATE TABLE main.airports_ref (
    id             bigserial PRIMARY KEY,
    iata           text NOT NULL,
    icao           text,
    name           text,
    city           text,
    country        text,
    country_code   text,
    longitude      double precision,
    latitude       double precision,
    type           text,
    elevation_ft   integer,
    region         text,
    greater_region text,
    continent_code text,
    runways        jsonb,
    ourairports_id bigint,
    created_at     timestamptz NOT NULL DEFAULT now()
)
"""

GRANTS_SQL = r"""
DO $do$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='grp_aviation_write') THEN
        EXECUTE 'GRANT USAGE ON SCHEMA main TO grp_aviation_write';
        EXECUTE 'GRANT SELECT ON main.airports_ref TO grp_aviation_write';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='grp_aixii_read') THEN
        EXECUTE 'GRANT USAGE ON SCHEMA main TO grp_aixii_read';
        EXECUTE 'GRANT SELECT ON main.airports_ref TO grp_aixii_read';
    END IF;
END $do$;
"""


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS main")
    op.execute(TABLE_SQL)
    op.execute('CREATE UNIQUE INDEX ix_airports_ref_iata ON main.airports_ref (iata)')
    op.execute('CREATE INDEX ix_airports_ref_country_code ON main.airports_ref (country_code)')
    op.execute(GRANTS_SQL)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS main.airports_ref")
