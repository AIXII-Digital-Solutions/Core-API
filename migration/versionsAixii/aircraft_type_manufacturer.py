"""An aircraft type is identified by manufacturer AND master series, not by the series alone

Cirium carries 806 distinct (Manufacturer, Master Series) pairs across Commercial and
Business & Helicopters, but only 751 distinct master series: 48 series are built by more than one
manufacturer. That is not dirty data, it is licence production — Kawasaki builds the BK117, Mitsubishi
the CRJ family and the UH-60, Harbin the ERJ-145, Indonesia Aerospace the CN235, Viking Air the
DHC-6. Two of those rows are the same design and different builds.

So `uq_aircraft_type_master_series` becomes `uq_aircraft_type_manufacturer_series` over the PAIR.
`manufacturer_normalized` is added as a STORED generated column to carry it, mirroring the two
columns that already exist, and the constraint is declared NULLS NOT DISTINCT so a series entered
without a manufacturer still collides with itself rather than being inserted twice.

CONSEQUENCE FOR THE WRITE PATH. Looking a type up by series alone can now return more than one row.
`Utils/DomainCommon.get_or_create_aircraft_type` therefore matches the pair when a manufacturer is
given, and by series alone otherwise — but only when exactly one row matches; an ambiguous series
without a manufacturer is rejected rather than resolved by guesswork.

Revision ID: aircraft_type_manufacturer
Revises: airlines_to_ref
Create Date: 2026-09-21
"""
from alembic import op

revision = "aircraft_type_manufacturer"
down_revision = "airlines_to_ref"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE fleet.aircraft_type ADD COLUMN manufacturer_normalized text "
        "GENERATED ALWAYS AS (upper(btrim(manufacturer))) STORED"
    )
    op.execute(
        "ALTER TABLE fleet.aircraft_type DROP CONSTRAINT IF EXISTS uq_aircraft_type_master_series"
    )
    op.execute(
        "ALTER TABLE fleet.aircraft_type ADD CONSTRAINT uq_aircraft_type_manufacturer_series "
        "UNIQUE NULLS NOT DISTINCT (manufacturer_normalized, master_series_normalized)"
    )
    op.execute(
        "COMMENT ON CONSTRAINT uq_aircraft_type_manufacturer_series ON fleet.aircraft_type IS "
        "'A type is the PAIR. 48 master series in Cirium are built by more than one manufacturer "
        "(Kawasaki BK117, Mitsubishi CRJ, Harbin ERJ-145, Viking DHC-6 ...), so the series alone "
        "cannot be the key. NULLS NOT DISTINCT keeps a series with no manufacturer unique too.'"
    )


def downgrade() -> None:
    # Collapsing back to one row per series can fail on real data, and should: it would have to
    # discard a licence builder. Left to whoever means it.
    op.execute(
        "ALTER TABLE fleet.aircraft_type DROP CONSTRAINT IF EXISTS uq_aircraft_type_manufacturer_series"
    )
    op.execute("ALTER TABLE fleet.aircraft_type DROP COLUMN IF EXISTS manufacturer_normalized")
    op.execute(
        "ALTER TABLE fleet.aircraft_type ADD CONSTRAINT uq_aircraft_type_master_series "
        "UNIQUE (master_series_normalized)"
    )
