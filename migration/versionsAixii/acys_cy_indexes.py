"""Contract Year index on forecast.acys_summary_by_day so PowerBI (and any query) can filter / slice by
"Contract Year" without a full scan. The acys_summary_grouped MATVIEW already carries ix_acys_grouped_cy.

ONLY acys_summary_by_day gets one. This originally also indexed "Contract Year" on acys_actuals and
acys_forecast; 19 days of live pg_stat_user_indexes then showed the reality:
    ix_acys_by_day_cy    1,445,925 scans   <- the reports read this table, keep it
    ix_acys_actuals_cy           0 scans,  97 MB   <- dropped (see forecast_geo_indexes)
    ix_acys_forecast_cy          0 scans, 3.1 MB   <- dropped
Nothing filters the row-level acys_actuals / acys_forecast by Contract Year — they are written per request
(DELETE + re-INSERT of ~300k rows), so those two indexes were pure write-amplification. They are removed here
so a re-apply of this migration does not resurrect them.

Revision ID: acys_cy_indexes
Revises: forecast_grouped_route_cols
Create Date: 2026-07-16
"""
from alembic import op

revision = "acys_cy_indexes"
down_revision = "forecast_grouped_route_cols"
branch_labels = None
depends_on = None

# index name -> "<schema.table> (<column>)"
_CY_INDEXES = {
    "ix_acys_by_day_cy": 'forecast.acys_summary_by_day ("Contract Year")',
}


def upgrade() -> None:
    for name, target in _CY_INDEXES.items():
        op.execute(f'CREATE INDEX IF NOT EXISTS {name} ON {target}')


def downgrade() -> None:
    for name in _CY_INDEXES:
        op.execute(f'DROP INDEX IF EXISTS forecast.{name}')
