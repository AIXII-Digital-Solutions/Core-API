"""Performance indexes for the forecast hot path.

The forecast run spends ~95% of its wall clock in the model step, whose route pool re-reads
forecast.acys_actuals once per (forecast month x sub-fleet) — 185 times for an Air Arabia run — filtering on
("Operator", "Aircraft Sub Series", "Date"). acys_actuals is 7.7M rows / 2.8 GB and only had an "Operator"
index, which for a large operator (Air Arabia = 301k rows) degrades to a LOSSY bitmap heap scan
(observed: "Heap Blocks: exact=38471 lossy=33322", "Rows Removed by Index Recheck: 717098").

  * ix_acys_actuals_op_ss_date — the route-pool / fit filter. "Date" is included so the very common
    `"Date" IS NOT NULL` + month/year extraction is answered from the index, and so the pool's per-route
    aggregation reads pre-sorted input.
  * ix_acys_actuals_op_route — the _route_impute_sql passes GROUP BY ("Operator","IATA Origin","IATA
    Destination") over the operator's rows.
  * ix_flightsummary_reg_first_seen — _assemble_sql's array6 CTE filters
    `reg IN (...) AND first_seen::date > :floor AND first_seen < :as_of`. flightsummary is 8.3M rows / 4.2 GB
    and had (reg, datetime_takeoff) but NOT (reg, first_seen), so the assemble Seq Scanned all 8.3M rows.

All three are created CONCURRENTLY inside an autocommit block: these are large, actively-read production
tables and a plain CREATE INDEX would hold an ACCESS EXCLUSIVE lock for the whole build. CONCURRENTLY cannot
run inside a transaction, hence the autocommit_block; it also means a failed build can leave an INVALID index
behind, so every statement is IF NOT EXISTS and the downgrade drops them the same way.

Revision ID: forecast_perf_indexes
Revises: acys_cy_indexes
Create Date: 2026-07-16
"""
from alembic import op

revision = "forecast_perf_indexes"
down_revision = "acys_cy_indexes"
branch_labels = None
depends_on = None


_INDEXES = [
    ('ix_acys_actuals_op_ss_date',
     'CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_acys_actuals_op_ss_date '
     'ON forecast.acys_actuals ("Operator", "Aircraft Sub Series", "Date")'),
    ('ix_acys_actuals_op_route',
     'CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_acys_actuals_op_route '
     'ON forecast.acys_actuals ("Operator", "IATA Origin", "IATA Destination")'),
    ('ix_flightsummary_reg_first_seen',
     'CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_flightsummary_reg_first_seen '
     'ON flightradar.flightsummary (reg, first_seen)'),
]


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for _, ddl in _INDEXES:
            op.execute(ddl)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, _ in _INDEXES:
            schema = "flightradar" if "flightsummary" in name else "forecast"
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {schema}.{name}")
