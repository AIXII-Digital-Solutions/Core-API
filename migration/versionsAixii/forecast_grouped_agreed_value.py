"""Add three Agreed-Value fields to forecast.acys_summary_grouped, per (Registration, Contract Year),
duplicated across that aircraft's route/month rows (by design):
  * "Agreed Value on Inception"                 — Agreed Value of the FIRST month of the contract year,
  * "Agreed Value at the End of the Contract"    — Agreed Value of the LAST month of the contract year,
  * "Weighted Average Agreed Value"              — TIME-weighted average over the contract year:
        Σ(value_month × days_of_month_within_the_CY_active_window) / Σ(days),
  * "Activity-Weighted Average Agreed Value"      — flight-weighted average over the contract year:
        Σ(value_month × flights_month) / Σ(flights_month).
Wet-lease months are IGNORED (only Dry months with a real Agreed Value > 0 count). Partial first/last
months are weighted by their actual days within the aircraft's active [min Date, max Date] span of the CY.

Revision ID: forecast_grouped_agreed_value
Revises: forecast_location_value_views
Create Date: 2026-07-09
"""
from alembic import op

revision = "forecast_grouped_agreed_value"
down_revision = "forecast_location_value_views"
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

# Dry-only monthly Agreed Value (>0) filter, reused for the value/span CTEs.
_DRY = "\"Date\" IS NOT NULL AND \"Lease Dry Wet\" IS DISTINCT FROM 'Wet' AND \"Agreed Value\" > 0"

_CY_CTE = f"""
WITH av AS (   -- per (aircraft, contract year, month): the Dry monthly Agreed Value + that month's flights
    SELECT "Registration" reg, "Contract Year" cy, date_trunc('month',"Date")::date mon,
           max("Agreed Value") v, count(*) flights
    FROM forecast.acys_summary_by_day WHERE {_DRY}
    GROUP BY 1, 2, 3
),
span AS (      -- per (aircraft, contract year): active date window (Dry months only)
    SELECT "Registration" reg, "Contract Year" cy, min("Date") lo, max("Date") hi
    FROM forecast.acys_summary_by_day WHERE {_DRY}
    GROUP BY 1, 2
),
cyv AS (       -- per (aircraft, contract year): inception / end / time- & activity-weighted averages
    SELECT av.reg, av.cy,
           (array_agg(av.v ORDER BY av.mon))[1]      AS inception,
           (array_agg(av.v ORDER BY av.mon DESC))[1] AS at_end,
           sum(av.v * (LEAST((av.mon + interval '1 month')::date - 1, span.hi)
                       - GREATEST(av.mon, span.lo) + 1))
             / nullif(sum(LEAST((av.mon + interval '1 month')::date - 1, span.hi)
                          - GREATEST(av.mon, span.lo) + 1), 0)  AS wavg,
           sum(av.v * av.flights) / nullif(sum(av.flights), 0)  AS awavg
    FROM av JOIN span ON span.reg = av.reg AND span.cy = av.cy
    GROUP BY av.reg, av.cy
)"""


def _view_sql(with_agreed: bool) -> str:
    cte = _CY_CTE if with_agreed else ""
    extra = ("""    max(cyv.inception) AS "Agreed Value on Inception",
    max(cyv.at_end)    AS "Agreed Value at the End of the Contract",
    max(cyv.wavg)      AS "Weighted Average Agreed Value",
    max(cyv.awavg)     AS "Activity-Weighted Average Agreed Value" """) if with_agreed else None
    join = ("LEFT JOIN cyv ON cyv.reg = s.\"Registration\" AND cyv.cy = s.\"Contract Year\""
            if with_agreed else "")
    agg = _AGG + ("," if with_agreed else "")
    return f"""
CREATE VIEW forecast.acys_summary_grouped AS{cte}
SELECT
    {_GROUP_COLS},
    to_date("Period", 'MM-YYYY') AS "Date",
{agg}
{extra + chr(10) if extra else ""}FROM forecast.acys_summary_by_day s
{join}
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
    op.execute(_view_sql(with_agreed=True))
    op.execute(_GRANT_VIEW)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS forecast.acys_summary_grouped")
    op.execute(_view_sql(with_agreed=False))
    op.execute(_GRANT_VIEW)
