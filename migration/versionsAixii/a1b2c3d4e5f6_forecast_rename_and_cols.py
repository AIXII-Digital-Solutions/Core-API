"""forecast: rename tables (acys_*) + Agreed Value/Total Seats/Total PAX/Actual Distance FR/Flight
Time FR columns + origin/dest lat/lon on the summary

Revision ID: a1b2c3d4e5f6
Revises: f0a1b2c3d4e5
Create Date: 2026-07-02

Renames the working tables:  history_1 -> acys_actuals, future_1 -> acys_forecast,
                             final_1 -> acys_summary  (+ their indexes / id sequences).

Adds to ALL THREE tables (schema-consistent; populated in acys_actuals, carried into acys_summary
by the merge):
  "Agreed Value"       double precision  -- Cirium "Indicative Market Value (US$m)"
  "Total Seats"        integer           -- Cirium "Number of Seats"
  "Total PAX"          double precision  -- Total Seats * load factor (FORECAST_PAX_LOAD_FACTOR, def 0.8)
  "Actual Distance FR" double precision  -- flightsummary.circle_distance
  "Flight Time FR"     interval          -- flightsummary.flight_time (seconds) -> interval

Adds origin/destination airport coordinates on acys_summary only (next to the existing Country/City/
Airport Name enrichment): origin_lat, origin_lon, dest_lat, dest_lon.
"""
from alembic import op

revision = "a1b2c3d4e5f6"
down_revision = "f0a1b2c3d4e5"
branch_labels = None
depends_on = None

# (old, new)
_RENAMES = [("history_1", "acys_actuals"), ("future_1", "acys_forecast"), ("final_1", "acys_summary")]

_NEW_COLS = [
    ('"Agreed Value"', "double precision"),
    ('"Total Seats"', "integer"),
    ('"Total PAX"', "double precision"),
    ('"Actual Distance FR"', "double precision"),
    ('"Flight Time FR"', "interval"),
]

_LATLON = ["origin_lat", "origin_lon", "dest_lat", "dest_lon"]


def upgrade() -> None:
    for old, new in _RENAMES:
        op.execute(f"ALTER TABLE forecast.{old} RENAME TO {new}")
        op.execute(f"ALTER INDEX IF EXISTS forecast.ix_forecast_{old}_operator "
                   f"RENAME TO ix_forecast_{new}_operator")
        op.execute(f"ALTER INDEX IF EXISTS forecast.ix_forecast_{old}_reg_period "
                   f"RENAME TO ix_forecast_{new}_reg_period")
        op.execute(f"ALTER SEQUENCE IF EXISTS forecast.{old}_id_seq RENAME TO {new}_id_seq")

    # new columns on all three (now renamed) tables
    for _old, new in _RENAMES:
        for col, typ in _NEW_COLS:
            op.execute(f'ALTER TABLE forecast.{new} ADD COLUMN {col} {typ}')

    # airport coordinates only on the enriched summary table
    for col in _LATLON:
        op.execute(f'ALTER TABLE forecast.acys_summary ADD COLUMN {col} double precision')


def downgrade() -> None:
    for col in _LATLON:
        op.execute(f'ALTER TABLE forecast.acys_summary DROP COLUMN IF EXISTS {col}')
    for _old, new in _RENAMES:
        for col, _typ in _NEW_COLS:
            op.execute(f'ALTER TABLE forecast.{new} DROP COLUMN IF EXISTS {col}')
    for old, new in _RENAMES:
        op.execute(f"ALTER SEQUENCE IF EXISTS forecast.{new}_id_seq RENAME TO {old}_id_seq")
        op.execute(f"ALTER INDEX IF EXISTS forecast.ix_forecast_{new}_reg_period "
                   f"RENAME TO ix_forecast_{old}_reg_period")
        op.execute(f"ALTER INDEX IF EXISTS forecast.ix_forecast_{new}_operator "
                   f"RENAME TO ix_forecast_{old}_operator")
        op.execute(f"ALTER TABLE forecast.{new} RENAME TO {old}")
