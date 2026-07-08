"""Split the forecast summary into two levels:
  * forecast.acys_summary_by_day  — the renamed acys_summary: ONE ROW PER FLIGHT (no grouping),
    keeps "Date" / "Time Departed" / "Time Landed", drops "# Of Flights".
  * forecast.acys_summary_grouped — a VIEW over acys_summary_by_day: one row per (aircraft, month,
    route) with "# Of Flights" = count and the four metric columns SUMMED, WITHOUT "Date" /
    "Time Departed" / "Time Landed". Consumed by PBI Direct Query.

The Wet rule (Agreed Value=0) and Age are already materialised per-flight in acys_summary_by_day by
the merge; the view only groups + sums (Age -> min of the group).

Revision ID: forecast_summary_split
Revises: forecast_summary_drop_date
Create Date: 2026-07-08
"""
from alembic import op

revision = "forecast_summary_split"
down_revision = "forecast_summary_drop_date"
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

_CREATE_VIEW = f"""
CREATE VIEW forecast.acys_summary_grouped AS
SELECT
    {_GROUP_COLS},
    min("Age")                AS "Age",
    count(*)                  AS "# Of Flights",
    sum("Circle Distance")    AS "Circle Distance",
    sum("Actual Distance FR") AS "Actual Distance FR",
    sum("Flight Time")        AS "Flight Time",
    sum("Flight Time FR")     AS "Flight Time FR"
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
    # per-flight table: restore Date (undo forecast_summary_drop_date), drop the grouped-only count
    op.execute('ALTER TABLE forecast.acys_summary ADD COLUMN IF NOT EXISTS "Date" date')
    op.execute('ALTER TABLE forecast.acys_summary DROP COLUMN IF EXISTS "# Of Flights"')
    op.execute('ALTER TABLE forecast.acys_summary RENAME TO acys_summary_by_day')
    # grouped view (PBI Direct Query)
    op.execute(_CREATE_VIEW)
    op.execute(_GRANT_VIEW)


def downgrade() -> None:
    op.execute('DROP VIEW IF EXISTS forecast.acys_summary_grouped')
    op.execute('ALTER TABLE forecast.acys_summary_by_day RENAME TO acys_summary')
    op.execute('ALTER TABLE forecast.acys_summary ADD COLUMN IF NOT EXISTS "# Of Flights" integer')
    op.execute('ALTER TABLE forecast.acys_summary DROP COLUMN IF EXISTS "Date"')
