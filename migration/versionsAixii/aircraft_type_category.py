"""fleet.aircraft_type gains a category, and the category joins the key

An aircraft type was identified by manufacturer and master series. That is not enough to insure
against: an Airbus A300-600 freighter and an A300-600 in passenger layout are the same airframe
doing different jobs, carrying different risk, and until now they shared one row — so half the
fleet pointed at a type that described the other half.

The category becomes part of the KEY:

    uq_aircraft_type_manufacturer_series
        (manufacturer_normalized, master_series_normalized)            ->
        (manufacturer_normalized, master_series_normalized, category)

so 'Airbus A300-600 Passenger' and 'Airbus A300-600 Cargo' are two rows, and every aircraft points
at the true one. `NULLS NOT DISTINCT` stays: it guards the manufacturer, and `category` is NOT NULL
and can never be the null in question.

THREE VALUES, deliberately coarse, mapped from Cirium's 49-value per-airframe `Primary Usage`:
passenger (airline AND business aviation), cargo (freight plus the convertibles, whose cargo deck
is the whole point), other (military, training, EMS, agriculture, survey — neither of the first
two, which is most of the catalogue by row count and almost none of it by fleet).

This revision only makes room. The column arrives as 'other' on all 806 existing rows, which is
wrong for nearly all of them and is meant to be: the values come from Cirium, not from a guess
baked into a migration, and `_admin/load_type_categories.py` fills them, splits the rows that need
splitting and re-points `fleet.aircraft`. Run it immediately after this upgrade.

Revision ID: aircraft_type_category
Revises: detail_anchor_actuals
Create Date: 2026-09-22
"""
from alembic import op

revision = "aircraft_type_category"
down_revision = "detail_anchor_actuals"
branch_labels = None
depends_on = None

_CONSTRAINT = "uq_aircraft_type_manufacturer_series"


def upgrade() -> None:
    # asyncpg takes one statement per execute, so each of these stands alone.
    op.execute("CREATE TYPE fleet.aircraft_category AS ENUM ('passenger', 'cargo', 'other')")
    op.execute(
        "ALTER TABLE fleet.aircraft_type "
        "ADD COLUMN category fleet.aircraft_category NOT NULL DEFAULT 'other'"
    )
    op.execute(
        "COMMENT ON COLUMN fleet.aircraft_type.category IS "
        "'What the airframe is for: passenger (airline AND business aviation), cargo (freight "
        "and the convertibles), or other (military, training, EMS, utility - neither of the "
        "first two). Part of the type''s identity: an A300-600 freighter and an A300-600 in "
        "passenger layout are separate rows.'"
    )
    op.execute("CREATE INDEX ix_fleet_aircraft_type_category ON fleet.aircraft_type (category)")

    # Widen the key. Dropped and recreated rather than altered: a unique constraint's columns
    # cannot be changed in place.
    op.execute(f"ALTER TABLE fleet.aircraft_type DROP CONSTRAINT {_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE fleet.aircraft_type ADD CONSTRAINT {_CONSTRAINT} "
        "UNIQUE NULLS NOT DISTINCT (manufacturer_normalized, master_series_normalized, category)"
    )


def downgrade() -> None:
    # Narrowing the key can fail, and should: once the catalogue is split, 'A300-600 Passenger'
    # and 'A300-600 Cargo' collide on the old two-column key. Collapse or delete the duplicates
    # first - there is no safe automatic answer to which of the two rows the aircraft belong to.
    op.execute(f"ALTER TABLE fleet.aircraft_type DROP CONSTRAINT {_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE fleet.aircraft_type ADD CONSTRAINT {_CONSTRAINT} "
        "UNIQUE NULLS NOT DISTINCT (manufacturer_normalized, master_series_normalized)"
    )
    op.execute("DROP INDEX fleet.ix_fleet_aircraft_type_category")
    op.execute("ALTER TABLE fleet.aircraft_type DROP COLUMN category")
    op.execute("DROP TYPE fleet.aircraft_category")
