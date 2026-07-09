"""forecast.acys_summary_grouped view: + "Date" (computed) = to_date("Period", 'MM-YYYY') — the first
day (always 01) of the group's Period month, as a real date. Computed on the already-grouped rows
(Period is a GROUP BY column). The view is DROP+CREATEd (to place Date with the dimension columns) and
its SELECT grants re-applied.

Revision ID: forecast_grouped_date
Revises: drop_predictive_util
Create Date: 2026-07-08
"""
from alembic import op

revision = "forecast_grouped_date"
down_revision = "drop_predictive_util"
branch_labels = None
depends_on = None

_GROUP_COLS = """"Registration","Period",
    "IATA Origin","IATA Destination","IATA Destination Actual",
    "ICAO Origin","ICAO Destination","ICAO Destination Actual",
    "Operator","Master Series","Manufacturer","Aircraft Sub Series","Primary Usage",
    "Contract Year",
    "Agreed Value","Total Seats","Total PAX",
    "Delivery Date","Lease Type","Lease Dry Wet","Operational Lessor",
    "Data Type",
    "Origin Country","Origin City","Origin Airport Name",
    "Destination Country","Destination City","Destination Airport Name",
    origin_lat, origin_lon, dest_lat, dest_lon"""

_AGG = """    min("Age")                AS "Age",
    count(*)                  AS "# Of Flights",
    sum("Circle Distance")    AS "Circle Distance",
    sum("Actual Distance FR") AS "Actual Distance FR",
    sum("Flight Time")        AS "Flight Time",
    sum("Flight Time FR")     AS "Flight Time FR" """


def _view_sql(with_date: bool) -> str:
    date_col = '    to_date("Period", \'MM-YYYY\') AS "Date",\n' if with_date else ""
    return f"""
CREATE VIEW forecast.acys_summary_grouped AS
SELECT
    {_GROUP_COLS},
{date_col}{_AGG}
FROM forecast.acys_summary_by_day
GROUP BY {_GROUP_COLS}
"""


_GRANT_VIEW = """
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aixii_read') THEN
    GRANT SELECT ON forecast.acys_summary_grouped TO grp_aixii_read;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aviation_write') THEN
    GRANT SELECT ON forecast.acys_summary_grouped TO grp_aviation_write;
  END IF;
END $$;
"""


def upgrade() -> None:
    op.execute("DROP VIEW IF EXISTS forecast.acys_summary_grouped")
    op.execute(_view_sql(with_date=True))
    op.execute(_GRANT_VIEW)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS forecast.acys_summary_grouped")
    op.execute(_view_sql(with_date=False))
    op.execute(_GRANT_VIEW)
