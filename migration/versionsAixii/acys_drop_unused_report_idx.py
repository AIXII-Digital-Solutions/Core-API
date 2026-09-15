"""Drop the report-matview indexes that nothing reads, to cut the time the rollup takes to rebuild

Every forecast run — and every restored snapshot — ends by rebuilding the report matviews, and a
rebuild rebuilds EVERY index on them: a plain REFRESH re-creates each one from scratch, a
CONCURRENT one maintains each one row by row for every changed row. acys_summary_grouped carried 23.
Measured on a copy of production data (1.2M rows), building them took 22s on top of the 16s the
rollup query itself takes: the indexes cost more than the data.

Thirteen of them had NO scans at all in pg_stat_user_indexes over the window measured (server up
since 2026-09-11, four days of normal PowerBI use), while the indexes that do serve the report ran to
a million scans each in the same window — so this is not a quiet period, it is these indexes going
unused. Mostly single low-selectivity columns ("Data Type", "Contract Year", "Operator") the planner
never picks for this data, and airport-name/IATA columns no slicer filters on.

Dropped (idx_scan = 0 as of 2026-09-15):
  acys_summary_grouped                   o_airport, d_airport, iata_o, iata_d, iata_da, od,
                                         operator, cy, dtype, agegroup
  acys_summary_grouped_by_reg            cy
  acys_summary_grouped_by_reg_and_year   cy, dtype

KEPT, deliberately, even where idx_scan is 0: every UNIQUE index. They are what REFRESH ... CONCURRENTLY
needs (see acys_matview_unique_keys); the scan counter does not count that use.

If a report visual gets slower after this, pg_stat_user_indexes will not help find it — the index is
gone. The downgrade restores each one with its exact original definition.

WARNING FOR THE NEXT MIGRATION THAT REBUILDS THE CHAIN. `forecast_grouped_route_cols._rebuild` (which
later migrations import) re-creates indexes from HARDCODED lists that still name these thirteen, and
that do NOT name the UNIQUE refresh keys from acys_matview_unique_keys. Rebuilding the chain through it
unchanged would bring these indexes back AND silently drop the unique keys — the second is the
dangerous half: without them a concurrent refresh is refused. Correct those lists in the migration that
does the rebuild, not after.

Revision ID: acys_drop_unused_report_idx
Revises: acys_matview_unique_keys
Create Date: 2026-09-15
"""
from alembic import op

revision = "acys_drop_unused_report_idx"
down_revision = "acys_matview_unique_keys"
branch_labels = None
depends_on = None

# Exact definitions captured from pg_indexes on 2026-09-15 — the downgrade replays them verbatim.
_INDEXES = {
    "ix_acys_grouped_agegroup":
        'CREATE INDEX ix_acys_grouped_agegroup ON forecast.acys_summary_grouped USING btree ("Age Group")',
    "ix_acys_grouped_cy":
        'CREATE INDEX ix_acys_grouped_cy ON forecast.acys_summary_grouped USING btree ("Contract Year")',
    "ix_acys_grouped_d_airport":
        'CREATE INDEX ix_acys_grouped_d_airport ON forecast.acys_summary_grouped USING btree ("Destination Airport Name")',
    "ix_acys_grouped_dtype":
        'CREATE INDEX ix_acys_grouped_dtype ON forecast.acys_summary_grouped USING btree ("Data Type")',
    "ix_acys_grouped_iata_d":
        'CREATE INDEX ix_acys_grouped_iata_d ON forecast.acys_summary_grouped USING btree ("IATA Destination")',
    "ix_acys_grouped_iata_da":
        'CREATE INDEX ix_acys_grouped_iata_da ON forecast.acys_summary_grouped USING btree ("IATA Destination Actual")',
    "ix_acys_grouped_iata_o":
        'CREATE INDEX ix_acys_grouped_iata_o ON forecast.acys_summary_grouped USING btree ("IATA Origin")',
    "ix_acys_grouped_o_airport":
        'CREATE INDEX ix_acys_grouped_o_airport ON forecast.acys_summary_grouped USING btree ("Origin Airport Name")',
    "ix_acys_grouped_od":
        'CREATE INDEX ix_acys_grouped_od ON forecast.acys_summary_grouped USING btree ("OD City&Country")',
    "ix_acys_grouped_operator":
        'CREATE INDEX ix_acys_grouped_operator ON forecast.acys_summary_grouped USING btree ("Operator")',
    "ix_by_reg_cy":
        'CREATE INDEX ix_by_reg_cy ON forecast.acys_summary_grouped_by_reg USING btree ("Contract Year")',
    "ix_by_reg_year_cy":
        'CREATE INDEX ix_by_reg_year_cy ON forecast.acys_summary_grouped_by_reg_and_year USING btree ("Contract Year")',
    "ix_by_reg_year_dtype":
        'CREATE INDEX ix_by_reg_year_dtype ON forecast.acys_summary_grouped_by_reg_and_year USING btree ("Data Type")',
}


def upgrade() -> None:
    # One statement per op.execute: asyncpg prepares each one.
    for name in _INDEXES:
        op.execute(f"DROP INDEX IF EXISTS forecast.{name}")


def downgrade() -> None:
    for name, ddl in _INDEXES.items():
        op.execute(ddl.replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ", 1))
