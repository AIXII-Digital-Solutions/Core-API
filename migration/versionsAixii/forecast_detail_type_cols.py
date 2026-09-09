"""forecast.detailed_aircraft_information gains the type ladder: Manufacturer, Master Series, Current Family.

All three come straight from forecast.aircraft_information — the same row the sheet already reads its MSN and
lease attributes from (the tail as of the last month of that aircraft-year) — and they sit next to
"Aircraft Type" (= Aircraft Sub Series), narrowest last: Manufacturer -> Master Series -> Current Family ->
Aircraft Type. With them the sheet groups at any level of the ladder without joining the dimension again.

"Current Family" is also the key the CSL is resolved through (powerbi.body_type_mapping), so having it on the
row makes a 650 next to a wide body self-explanatory: the tails that get one are exactly those Cirium has no
current family for — undelivered airframes on order.

A column cannot be inserted mid-view with CREATE OR REPLACE, so the view is dropped and recreated. Both
shapes come from the source of truth forecast_grouped_route_cols._detailed_ac_info: `type_cols=True` here,
`type_cols=False` for the shape the previous revision built, which is what downgrade puts back.

Revision ID: forecast_detail_type_cols
Revises: forecast_detailed_ac_info_musd
Create Date: 2026-09-09
"""
import os
import sys

from alembic import op

sys.path.insert(0, os.path.dirname(__file__))
from forecast_grouped_route_cols import _detailed_ac_info  # noqa: E402

revision = "forecast_detail_type_cols"
down_revision = "forecast_detailed_ac_info_musd"
branch_labels = None
depends_on = None

_VIEW = "forecast.detailed_aircraft_information"

_GRANTS = f"""
DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['grp_aixii_read','grp_aviation_write'] LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('GRANT SELECT ON {_VIEW} TO %I', r);
    END IF;
  END LOOP;
END $$;
"""


def _rebuild(type_cols: bool) -> None:
    # musd=True on both sides: the millions/text-YOM shape is what the previous revision left behind.
    op.execute(f"DROP VIEW IF EXISTS {_VIEW}")
    op.execute(_detailed_ac_info(musd=True, type_cols=type_cols))
    op.execute(_GRANTS)


def upgrade() -> None:
    _rebuild(type_cols=True)


def downgrade() -> None:
    _rebuild(type_cols=False)
