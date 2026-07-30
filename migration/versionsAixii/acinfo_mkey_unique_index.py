"""forecast.aircraft_information.MERGED_KEY -> UNIQUE index (Power BI dimension key).

aircraft_information's grain is exactly (Registration, "Aircraft Sub Series", Period) — the three
parts of MERGED_KEY — so MERGED_KEY is its natural UNIQUE key. Power BI relates the aircraft
dimension on this column; with only a plain index a DirectQuery model that relates on a non-unique
column errored ("Duplicate values are detected from data source"). A UNIQUE index documents/enforces
the key at the DB and lets the matview survive a CONCURRENT refresh later.

Data verified unique before this runs: 0 duplicate MERGED_KEY (incl. lower()/btrim() folding). The
canonical index list (forecast_grouped_route_cols._AIRCRAFT_INFO_INDEXES) was updated in lockstep so
future drop+recreate migrations keep ix_acinfo_mkey UNIQUE.

CAVEAT: the only collision vector is a NULL vs '' "Aircraft Sub Series" for one Registration+Period
(GROUP BY keeps them apart, MERGED_KEY's COALESCE(...,'') folds them) — then REFRESH would fail
loudly. Impossible on current data (0 empty-string sub series); harden by COALESCEing the sub series
in the matview GROUP BY if it ever appears.

Revision ID: acinfo_mkey_unique
Revises: flightaware_history
Create Date: 2026-07-31
"""
from alembic import op

revision = "acinfo_mkey_unique"
down_revision = "flightaware_history"
branch_labels = None
depends_on = None

_IX = "forecast.ix_acinfo_mkey"
_MV = "forecast.aircraft_information"


def upgrade() -> None:
    # replace the existing plain ix_acinfo_mkey with a UNIQUE one (same name)
    op.execute(f"DROP INDEX IF EXISTS {_IX}")
    op.execute(f'CREATE UNIQUE INDEX ix_acinfo_mkey ON {_MV} ("MERGED_KEY")')


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_IX}")
    op.execute(f'CREATE INDEX ix_acinfo_mkey ON {_MV} ("MERGED_KEY")')
