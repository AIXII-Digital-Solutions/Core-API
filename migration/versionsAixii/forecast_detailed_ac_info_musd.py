"""forecast.detailed_aircraft_information: the Agreed-Value columns actually in mUSD, and a text YOM.

The four columns are named "/ mUSD" but carried raw dollars — 57500000 where the sheet reads 57.50. They are
now divided by a million and rounded to cents, so the number matches the unit in its own name and no report
has to divide it again. Rounding to 2 makes them NUMERIC(.,2) rather than double precision: cents are exact,
and a value already denominated in millions has nothing below a cent worth keeping.

"YOM" becomes text (2012 -> '2012'): it is a label, not a quantity — nothing sums or averages a year, while a
numeric column invites a BI tool to total it or to format it with a thousands separator.

A view cannot change a column's type in place (CREATE OR REPLACE keeps the old ones), so it is dropped and
recreated. Both shapes come from the same source of truth — forecast_grouped_route_cols._detailed_ac_info,
`musd=True` here and `musd=False` for the shape the previous revision introduced, which is what downgrade
puts back.

Revision ID: forecast_detailed_ac_info_musd
Revises: forecast_detailed_aircraft_info
Create Date: 2026-09-09
"""
import os
import sys

from alembic import op

sys.path.insert(0, os.path.dirname(__file__))
from forecast_grouped_route_cols import _detailed_ac_info  # noqa: E402

revision = "forecast_detailed_ac_info_musd"
down_revision = "forecast_detailed_aircraft_info"
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


def _rebuild(musd: bool) -> None:
    # type_cols=False on both sides: the Manufacturer / Master Series / Current Family columns belong to
    # forecast_detail_type_cols, a later revision, not to this one.
    op.execute(f"DROP VIEW IF EXISTS {_VIEW}")
    op.execute(_detailed_ac_info(musd=musd, type_cols=False))
    op.execute(_GRANTS)


def upgrade() -> None:
    _rebuild(musd=True)


def downgrade() -> None:
    _rebuild(musd=False)
