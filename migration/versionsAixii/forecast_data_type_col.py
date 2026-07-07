"""forecast.acys_summary: + Data Type ('Actuals' vs 'Forecast') — tags each summary row by the panel
branch it came from (acys_actuals -> 'Actuals', acys_forecast -> 'Forecast'). Set by the merge step.

Revision ID: forecast_data_type
Revises: forecast_lease_cols
Create Date: 2026-07-08
"""
from alembic import op

revision = "forecast_data_type"
down_revision = "forecast_lease_cols"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute('ALTER TABLE forecast.acys_summary ADD COLUMN "Data Type" text')


def downgrade() -> None:
    op.execute('ALTER TABLE forecast.acys_summary DROP COLUMN IF EXISTS "Data Type"')
