"""forecast.acys_summary: + "# Of Flights" (integer) — count of grouped flights (same aircraft,
month, route). Set by the merge step, which now GROUPs the summary and sums the metric columns.

Revision ID: forecast_num_flights
Revises: forecast_data_type
Create Date: 2026-07-08
"""
from alembic import op

revision = "forecast_num_flights"
down_revision = "forecast_data_type"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute('ALTER TABLE forecast.acys_summary ADD COLUMN "# Of Flights" integer')


def downgrade() -> None:
    op.execute('ALTER TABLE forecast.acys_summary DROP COLUMN IF EXISTS "# Of Flights"')
