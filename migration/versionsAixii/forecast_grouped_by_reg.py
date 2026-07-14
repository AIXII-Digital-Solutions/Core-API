"""forecast.acys_summary_grouped_by_reg — acys_summary_grouped rolled up one level further: every
route/geography column is dropped, so the grain becomes the AIRCRAFT (Registration × Period × Contract Year
× Data Type) instead of the aircraft's individual routes.

Dropped: IATA/ICAO Origin, IATA/ICAO Destination, IATA/ICAO Destination Actual, Origin & Destination
Country / City / Airport Name, origin_lat, origin_lon, dest_lat, dest_lon.

Built as a VIEW on top of the acys_summary_grouped MATVIEW (not re-derived from acys_summary_by_day): it
re-aggregates ~184k rows down to the per-aircraft grain in milliseconds, and it automatically follows the
matview's refresh, so the two can never disagree.

Aggregation:
  * "# Of Flights" / distances / flight times  -> SUM (they are additive across the routes of one aircraft)
  * "Age"                                      -> MIN (as in the parent view)
  * the four Agreed-Value columns              -> MAX; they are per (Registration, Contract Year) and are
    therefore CONSTANT across every route row of the same aircraft-year, so MAX just picks that one value.

Revision ID: forecast_grouped_by_reg
Revises: forecast_flight_time_hours
Create Date: 2026-07-10
"""
from alembic import op

revision = "forecast_grouped_by_reg"
down_revision = "forecast_flight_time_hours"
branch_labels = None
depends_on = None

_KEYS = """    "Registration", "Period",
    "Operator", "Master Series", "Manufacturer", "Aircraft Sub Series", "Primary Usage",
    "Contract Year",
    "Agreed Value", "Total Seats", "Total PAX",
    "Delivery Date", "Lease Type", "Lease Dry Wet", "Operational Lessor",
    "Data Type",
    "Date\""""

_VIEW = f"""
CREATE VIEW forecast.acys_summary_grouped_by_reg AS
SELECT
{_KEYS},
    min("Age")                                     AS "Age",
    sum("# Of Flights")                            AS "# Of Flights",
    sum("Circle Distance")                         AS "Circle Distance",
    sum("Actual Distance FR")                      AS "Actual Distance FR",
    sum("Flight Time")                             AS "Flight Time",
    sum("Flight Time FR")                          AS "Flight Time FR",
    max("Agreed Value on Inception")               AS "Agreed Value on Inception",
    max("Agreed Value at the End of the Contract") AS "Agreed Value at the End of the Contract",
    max("Weighted Average Agreed Value")           AS "Weighted Average Agreed Value",
    max("Activity-Weighted Average Agreed Value")  AS "Activity-Weighted Average Agreed Value"
FROM forecast.acys_summary_grouped
GROUP BY
{_KEYS}
"""

_GRANTS = """
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aixii_read') THEN
    GRANT SELECT ON forecast.acys_summary_grouped_by_reg TO grp_aixii_read;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aviation_write') THEN
    GRANT SELECT ON forecast.acys_summary_grouped_by_reg TO grp_aviation_write;
  END IF;
END $$;
"""


def upgrade() -> None:
    op.execute(_VIEW)
    op.execute(_GRANTS)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS forecast.acys_summary_grouped_by_reg")
