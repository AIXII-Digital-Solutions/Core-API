"""Turn forecast.acys_summary_grouped from a VIEW into a MATERIALIZED VIEW (same SQL/columns), so PowerBI
reads a physical rollup instead of re-aggregating acys_summary_by_day on every query. The worker REFRESHes
it right after the merge fills acys_summary_by_day.

OWNERSHIP MATTERS: in Postgres only the OWNER (or a member of the owning role) may REFRESH a materialized
view — it is not a GRANTable privilege. Migrations run as `developer`, but the worker runs as
`svc_external_worker` (a member of grp_aviation_write). So the matview is handed to grp_aviation_write,
which makes the worker able to refresh it while bi_reader/grp_aixii_read keep plain SELECT.

Not REFRESH ... CONCURRENTLY: that needs a UNIQUE index, and the natural key here is the full 32-column
GROUP BY set (with nullable columns) — no usable unique key. A plain REFRESH takes a brief ACCESS EXCLUSIVE
lock; at ~180k rows it is a couple of seconds, right after a per-request rebuild.

Revision ID: forecast_grouped_matview
Revises: forecast_coefficients_table
Create Date: 2026-07-10
"""
from alembic import op

revision = "forecast_grouped_matview"
down_revision = "forecast_coefficients_table"
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

_WAVG = ("((array_agg(av.v ORDER BY av.mon))[1] + "
         "(array_agg(av.v ORDER BY av.mon DESC))[1]) / 2.0")


def _body() -> str:
    """The SELECT that defines acys_summary_grouped (identical for the view and the matview)."""
    return f"""
WITH av AS (
    SELECT "Registration" reg, "Contract Year" cy, date_trunc('month',"Date")::date mon,
           max("Agreed Value") v, count(*) flights
    FROM forecast.acys_summary_by_day WHERE {_DRY}
    GROUP BY 1, 2, 3
),
cyv AS (
    SELECT av.reg, av.cy,
           (array_agg(av.v ORDER BY av.mon))[1]      AS inception,
           (array_agg(av.v ORDER BY av.mon DESC))[1] AS at_end,
           {_WAVG}                                    AS wavg,
           sum(av.v * av.flights) / nullif(sum(av.flights), 0)  AS awavg
    FROM av
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


# Hand the matview to grp_aviation_write so the worker (svc_external_worker, a member) can REFRESH it.
_OWNER = """
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aviation_write') THEN
    EXECUTE 'ALTER MATERIALIZED VIEW forecast.acys_summary_grouped OWNER TO grp_aviation_write';
  END IF;
END $$;
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
    op.execute(f"CREATE MATERIALIZED VIEW forecast.acys_summary_grouped AS{_body()}")
    # slicer/filter columns PowerBI hits most
    op.execute('CREATE INDEX ix_acys_grouped_operator ON forecast.acys_summary_grouped ("Operator")')
    op.execute('CREATE INDEX ix_acys_grouped_cy ON forecast.acys_summary_grouped ("Contract Year")')
    op.execute('CREATE INDEX ix_acys_grouped_reg ON forecast.acys_summary_grouped ("Registration")')
    op.execute('CREATE INDEX ix_acys_grouped_dtype ON forecast.acys_summary_grouped ("Data Type")')
    op.execute(_OWNER)
    op.execute(_GRANT)


def downgrade() -> None:
    op.execute("DROP MATERIALIZED VIEW IF EXISTS forecast.acys_summary_grouped")
    op.execute(f"CREATE VIEW forecast.acys_summary_grouped AS{_body()}")
    op.execute(_GRANT)
