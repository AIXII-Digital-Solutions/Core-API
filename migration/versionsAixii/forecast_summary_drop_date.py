"""forecast.acys_summary: drop the "Date" column. acys_summary is GROUPED (one row per aircraft +
month + route), so a single flight Date is meaningless there. Age is still derived from min(Date) of
the group inside the merge (acys_actuals keeps its per-flight "Date").

Revision ID: forecast_summary_drop_date
Revises: forecast_num_flights
Create Date: 2026-07-08
"""
from alembic import op
import sqlalchemy as sa

revision = "forecast_summary_drop_date"
down_revision = "forecast_num_flights"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute('ALTER TABLE forecast.acys_summary DROP COLUMN IF EXISTS "Date"')


def downgrade() -> None:
    op.add_column("acys_summary", sa.Column("Date", sa.Date(), nullable=True), schema="forecast")
