"""acys_forecast_coefficients += "History Key" — WHOSE flight history the fit (Level / Base Fleet / Per
Aircraft Rate / Seasonal Factor / route template) was taken from.

An operator can take delivery of a sub-series it has NEVER flown (Air Arabia is getting 12 A321-253N neo
ACF and has zero flights on the type), so there is no history to fit and no route pool to draw from. In
that case the model falls back to the aircraft's MASTER SERIES history. That substitution must not be
silent — the coefficients table exists to explain the forecast, so it records the key it actually used:

    "History Key" = "Aircraft Sub Series"   -> the sub-fleet's own history
    "History Key" = the Master Series name  -> fallback (no sub-series history existed)

Revision ID: forecast_coeff_history_key
Revises: forecast_grouped_by_reg
Create Date: 2026-07-10
"""
from alembic import op

revision = "forecast_coeff_history_key"
down_revision = "forecast_grouped_by_reg"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute('ALTER TABLE forecast.acys_forecast_coefficients ADD COLUMN "History Key" text')


def downgrade() -> None:
    op.execute('ALTER TABLE forecast.acys_forecast_coefficients DROP COLUMN IF EXISTS "History Key"')
