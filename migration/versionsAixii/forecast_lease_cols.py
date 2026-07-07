"""forecast.acys_*: + Delivery Date / Lease Type / Lease Dry Wet / Operational Lessor (sourced from
Cirium: ciriumaircrafts "Delivery Date" / "Lease Type" / "Lease Dry / Wet" / "Operational Lessor").

acys_summary additionally gets Age = decimal years between the flight Date and the Delivery Date
((Date - Delivery Date)/365.25). The "Wet" rule for acys_summary (Agreed Value forced to 0 when
Lease Dry Wet = 'Wet') is applied by the merge step, not stored as a column default here.

Revision ID: forecast_lease_cols
Revises: airport_city_ovr
Create Date: 2026-07-08
"""
from alembic import op

revision = "forecast_lease_cols"
down_revision = "airport_city_ovr"
branch_labels = None
depends_on = None

_TABLES = ("acys_actuals", "acys_forecast", "acys_summary")
# (quoted column, type) pulled from Cirium into EVERY forecast table
_LEASE_COLS = (
    ('"Delivery Date"', "date"),
    ('"Lease Type"', "text"),
    ('"Lease Dry Wet"', "text"),
    ('"Operational Lessor"', "text"),
)


def upgrade() -> None:
    for t in _TABLES:
        for col, typ in _LEASE_COLS:
            op.execute(f'ALTER TABLE forecast.{t} ADD COLUMN {col} {typ}')
    # Age (decimal years) — acys_summary ONLY
    op.execute('ALTER TABLE forecast.acys_summary ADD COLUMN "Age" numeric')


def downgrade() -> None:
    op.execute('ALTER TABLE forecast.acys_summary DROP COLUMN IF EXISTS "Age"')
    for t in _TABLES:
        for col, _typ in _LEASE_COLS:
            op.execute(f'ALTER TABLE forecast.{t} DROP COLUMN IF EXISTS {col}')
