"""Contract Year indexes on the forecast BASE TABLES so PowerBI (and any query) can filter / slice by
"Contract Year" without a full scan. The acys_summary_grouped MATVIEW already carries ix_acys_grouped_cy;
these cover the row-level tables it and the reports read from.

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
    "ix_acys_by_day_cy":   'forecast.acys_summary_by_day ("Contract Year")',
    "ix_acys_actuals_cy":  'forecast.acys_actuals ("Contract Year")',
    "ix_acys_forecast_cy": 'forecast.acys_forecast ("Contract Year")',
}


def upgrade() -> None:
    for name, target in _CY_INDEXES.items():
        op.execute(f'CREATE INDEX IF NOT EXISTS {name} ON {target}')


def downgrade() -> None:
    for name in _CY_INDEXES:
        op.execute(f'DROP INDEX IF EXISTS forecast.{name}')
