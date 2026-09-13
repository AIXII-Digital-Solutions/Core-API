"""Unique keys on the report matviews, so they can be refreshed without locking PowerBI out

A plain REFRESH MATERIALIZED VIEW takes ACCESS EXCLUSIVE. That waits for every open report query to
finish AND queues every new one behind it, so a single DirectQuery held open for minutes freezes the
forecast run at its last step and takes the report down with it while it waits. Observed in
production: a `bi_reader` query 13 minutes into its transaction, two runs abandoned at step 10.

REFRESH ... CONCURRENTLY takes no exclusive lock — readers keep reading throughout — but PostgreSQL
requires a UNIQUE index on the matview to do the row-by-row diff. Four of the seven report matviews
already had one; these are the three that did not.

THE KEYS ARE THE MATVIEWS' OWN GRAIN, not invented surrogates, so no definition changes and no
rebuild of the dependent chain. Each was verified unique against production data before being
written here (counts as of 2026-09-13):

  acys_summary_grouped          (MERGED_KEY, ROUTE_KEY, Data Type)             743,564 of 743,564
  acys_summary_grouped_by_reg   (MERGED_KEY, Data Type, Contract Year)          22,978 of 22,978
  powerbi.z_dates_acys          (Date, Contract Year, Data Type)                 2,192 of 2,192

Uniqueness was measured with COUNT(DISTINCT (...)), which treats NULLs as EQUAL — a stricter test
than the unique index itself applies, so a NULL in a key column cannot produce the duplicate rows
that would make CONCURRENTLY fail at runtime.

Plain CREATE INDEX, not CONCURRENTLY: it takes SHARE, which blocks a refresh but NOT readers, and
CREATE INDEX CONCURRENTLY cannot run inside Alembic's transaction anyway. Seconds on this data.

Revision ID: acys_matview_unique_keys
Revises: acys_snapshots_fingerprint
Create Date: 2026-09-13
"""
from alembic import op

revision = "acys_matview_unique_keys"
down_revision = "acys_snapshots_fingerprint"
branch_labels = None
depends_on = None

_INDEXES = [
    ('uq_acys_grouped_refresh', 'forecast.acys_summary_grouped',
     '"MERGED_KEY", "ROUTE_KEY", "Data Type"'),
    ('uq_acys_by_reg_refresh', 'forecast.acys_summary_grouped_by_reg',
     '"MERGED_KEY", "Data Type", "Contract Year"'),
    ('uq_zdates_acys_refresh', 'powerbi.z_dates_acys',
     '"Date", "Contract Year", "Data Type"'),
]


def upgrade() -> None:
    for name, table, cols in _INDEXES:
        op.execute(f"CREATE UNIQUE INDEX {name} ON {table} ({cols})")


def downgrade() -> None:
    # The index name is schema-qualified on the way out: it lives in its table's schema.
    for name, table, _cols in _INDEXES:
        schema = table.split(".", 1)[0]
        op.execute(f"DROP INDEX IF EXISTS {schema}.{name}")
