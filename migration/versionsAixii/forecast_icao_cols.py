"""forecast.acys_*: + ICAO Origin / ICAO Destination / ICAO Destination Actual (for the icao fallback
in airport enrichment)

Revision ID: forecast_icao_cols
Revises: airports_rename_views
Create Date: 2026-07-02
"""
from alembic import op

revision = "forecast_icao_cols"
down_revision = "airports_rename_views"
branch_labels = None
depends_on = None

_TABLES = ("acys_actuals", "acys_forecast", "acys_summary")
_COLS = ('"ICAO Origin"', '"ICAO Destination"', '"ICAO Destination Actual"')


def upgrade() -> None:
    for t in _TABLES:
        for col in _COLS:
            op.execute(f'ALTER TABLE forecast.{t} ADD COLUMN {col} text')


def downgrade() -> None:
    for t in _TABLES:
        for col in _COLS:
            op.execute(f'ALTER TABLE forecast.{t} DROP COLUMN IF EXISTS {col}')
