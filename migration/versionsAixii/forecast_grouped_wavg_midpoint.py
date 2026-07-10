"""Redefine forecast.acys_summary_grouped so "Weighted Average Agreed Value" is BOUNDED between the
inception and end-of-contract values: wavg = (inception + at_end) / 2. This is the time-weighted average
of a straight line from the CY's first to its last month value, so it is ALWAYS between start and end and
is robust to transient mid-year market spikes in the raw Cirium value (which previously made the true
day-weighted average exceed both endpoints). Monthly values in acys_summary_by_day stay real; only this
CY-level summary column changes. Inception / at_end / activity-weighted columns are unchanged.

Revision ID: forecast_grouped_wavg_midpoint
Revises: forecast_z_constant_matviews
Create Date: 2026-07-10
"""
from alembic import op

revision = "forecast_grouped_wavg_midpoint"
down_revision = "forecast_z_constant_matviews"
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

_DRY = "\"Date\" IS NOT NULL AND \"Lease Dry Wet\" IS DISTINCT FROM 'Wet' AND \"Agreed Value\" > 0"

# wavg variants: midpoint (new) vs the old day-weighted average (for downgrade).
_WAVG_MIDPOINT = ("((array_agg(av.v ORDER BY av.mon))[1] + "
                  "(array_agg(av.v ORDER BY av.mon DESC))[1]) / 2.0")
_WAVG_DAYWEIGHTED = """sum(av.v * (LEAST((av.mon + interval '1 month')::date - 1, span.hi)
                       - GREATEST(av.mon, span.lo) + 1))
             / nullif(sum(LEAST((av.mon + interval '1 month')::date - 1, span.hi)
                          - GREATEST(av.mon, span.lo) + 1), 0)"""


def _view_sql(midpoint: bool) -> str:
    # the day-weighted form needs the `span` (active [min,max] Date window); the midpoint form does not.
    span_cte = "" if midpoint else f""",
span AS (
    SELECT "Registration" reg, "Contract Year" cy, min("Date") lo, max("Date") hi
    FROM forecast.acys_summary_by_day WHERE {_DRY}
    GROUP BY 1, 2
)"""
    span_join = "" if midpoint else "JOIN span ON span.reg = av.reg AND span.cy = av.cy"
    wavg = _WAVG_MIDPOINT if midpoint else _WAVG_DAYWEIGHTED
    return f"""
CREATE VIEW forecast.acys_summary_grouped AS
WITH av AS (
    SELECT "Registration" reg, "Contract Year" cy, date_trunc('month',"Date")::date mon,
           max("Agreed Value") v, count(*) flights
    FROM forecast.acys_summary_by_day WHERE {_DRY}
    GROUP BY 1, 2, 3
){span_cte},
cyv AS (
    SELECT av.reg, av.cy,
           (array_agg(av.v ORDER BY av.mon))[1]      AS inception,
           (array_agg(av.v ORDER BY av.mon DESC))[1] AS at_end,
           {wavg}                                     AS wavg,
           sum(av.v * av.flights) / nullif(sum(av.flights), 0)  AS awavg
    FROM av {span_join}
    GROUP BY av.reg, av.cy
)
SELECT
    {_GROUP_COLS},
    to_date("Period", 'MM-YYYY') AS "Date",
{_AGG},
    max(cyv.inception) AS "Agreed Value on Inception",
    max(cyv.at_end)    AS "Agreed Value at the End of the Contract",
    max(cyv.wavg)      AS "Weighted Average Agreed Value",
    max(cyv.awavg)     AS "Activity-Weighted Average Agreed Value"
FROM forecast.acys_summary_by_day s
LEFT JOIN cyv ON cyv.reg = s."Registration" AND cyv.cy = s."Contract Year"
GROUP BY {_GROUP_COLS}
"""


_GRANT = """
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
    op.execute(_view_sql(midpoint=True))
    op.execute(_GRANT)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS forecast.acys_summary_grouped")
    op.execute(_view_sql(midpoint=False))
    op.execute(_GRANT)
