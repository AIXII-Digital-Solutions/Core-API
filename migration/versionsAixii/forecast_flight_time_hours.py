"""Flight Time / Flight Time FR: interval -> DECIMAL HOURS (double precision).

`interval` is the wrong type for a BI measure: PowerBI has no interval, it arrives as text or a duration
that will not SUM or AVERAGE, and every consumer had to unwrap it with extract(epoch ...)/3600. Stored as
decimal hours (6.51 = 6h31m) the columns add up and average natively.

Values are converted in place (extract(epoch)/3600), NOT recomputed — no data is lost or re-derived.
Full precision is kept (1h42m15s -> 1.704166…, not 1.7): rounding each flight before summing would drift
a Contract Year's block hours. Format the display in BI, not the storage.

DEPENDENCY DANCE: Postgres refuses ALTER COLUMN TYPE while a view/matview depends on the column, so the
whole chain must come down and go back up:
    powerbi.z_dates_acys -> forecast.z_contract_years -> forecast.acys_summary_grouped (matview)
The matview/view bodies below are verbatim copies of forecast_grouped_matview / forecast_z_contract_years /
powerbi_z_dates_acys_months — a migration is a snapshot, so they are duplicated rather than imported.

Revision ID: forecast_flight_time_hours
Revises: powerbi_z_dates_acys_months
Create Date: 2026-07-10
"""
from alembic import op

revision = "forecast_flight_time_hours"
down_revision = "powerbi_z_dates_acys_months"
branch_labels = None
depends_on = None

_TABLES = ("acys_actuals", "acys_forecast", "acys_summary_by_day")
_COLS = ('"Flight Time"', '"Flight Time FR"')

# ── verbatim rebuild of the dependent chain ────────────────────────────────────────────────────────────
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

_GROUPED_BODY = f"""
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

_Z_CONTRACT_YEARS = """
CREATE VIEW forecast.z_contract_years AS
SELECT DISTINCT "Contract Year"
FROM forecast.acys_summary_grouped
WHERE "Contract Year" IS NOT NULL
ORDER BY 1
"""

_Z_DATES_ACYS = """
CREATE VIEW powerbi.z_dates_acys AS
SELECT d.*
FROM powerbi.z_dates d
CROSS JOIN (
    SELECT date_trunc('month', min("Date"))::date                      AS lo,
           (date_trunc('month', max("Date")) + INTERVAL '1 month'
                                             - INTERVAL '1 day')::date AS hi
    FROM forecast.acys_summary_grouped
) b
WHERE d."Date" >= coalesce(b.lo, DATE '2022-07-01')
  AND d."Date" <= coalesce(b.hi, DATE '2029-12-31')
"""

_OWNER = """
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aviation_write') THEN
    EXECUTE 'ALTER MATERIALIZED VIEW forecast.acys_summary_grouped OWNER TO grp_aviation_write';
  END IF;
END $$;
"""

_GRANTS = """
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aixii_read') THEN
    GRANT SELECT ON forecast.acys_summary_grouped, forecast.z_contract_years,
                    powerbi.z_dates_acys TO grp_aixii_read;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aviation_write') THEN
    GRANT SELECT ON forecast.acys_summary_grouped, forecast.z_contract_years,
                    powerbi.z_dates_acys TO grp_aviation_write;
  END IF;
END $$;
"""


def _drop_chain() -> None:
    op.execute("DROP VIEW IF EXISTS powerbi.z_dates_acys")
    op.execute("DROP VIEW IF EXISTS forecast.z_contract_years")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS forecast.acys_summary_grouped")


def _rebuild_chain() -> None:
    op.execute(f"CREATE MATERIALIZED VIEW forecast.acys_summary_grouped AS{_GROUPED_BODY}")
    op.execute('CREATE INDEX ix_acys_grouped_operator ON forecast.acys_summary_grouped ("Operator")')
    op.execute('CREATE INDEX ix_acys_grouped_cy ON forecast.acys_summary_grouped ("Contract Year")')
    op.execute('CREATE INDEX ix_acys_grouped_reg ON forecast.acys_summary_grouped ("Registration")')
    op.execute('CREATE INDEX ix_acys_grouped_dtype ON forecast.acys_summary_grouped ("Data Type")')
    op.execute('CREATE INDEX ix_acys_grouped_date ON forecast.acys_summary_grouped ("Date")')
    op.execute(_OWNER)
    op.execute(_Z_CONTRACT_YEARS)
    op.execute(_Z_DATES_ACYS)
    op.execute(_GRANTS)


def upgrade() -> None:
    _drop_chain()
    for t in _TABLES:
        for c in _COLS:
            op.execute(f'ALTER TABLE forecast.{t} ALTER COLUMN {c} TYPE double precision '
                       f'USING extract(epoch from {c}) / 3600.0')
    _rebuild_chain()


def downgrade() -> None:
    _drop_chain()
    for t in _TABLES:
        for c in _COLS:
            op.execute(f'ALTER TABLE forecast.{t} ALTER COLUMN {c} TYPE interval '
                       f"USING ({c} * interval '1 hour')")
    _rebuild_chain()
