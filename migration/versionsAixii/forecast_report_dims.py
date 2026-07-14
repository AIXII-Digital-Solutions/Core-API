"""Report-shape changes, all four in one pass so the dependent chain is torn down and rebuilt ONCE:

 1. forecast.z_contract_years is DROPPED. Its job (offer the distinct Contract Years) moves into
    powerbi.z_dates_acys, which now carries a "Contract Year" column computed per calendar day — so the
    calendar itself answers "which CY is this date in", and PBI needs no separate lookup.
    The CY anchor is derived FROM THE DATA: actuals run to yesterday and the forecast starts today, so
    `min("Date") WHERE "Data Type"='Forecast'` IS the request date. Same day-precise rule as the model.

 2. acys_summary_by_day / acys_summary_grouped gain "Origin City&Country" and "Destination City&Country"
    ('Attock (Pakistan)'). On the TABLE they are GENERATED ... STORED columns, derived from the "Origin
    City"/"Origin Country" pair that is already there — so the 400k existing rows are filled the moment the
    column is added, every future insert fills itself, and no worker code has to know about them.

 3. acys_summary_grouped gains "MERGED_KEY" = Registration | Aircraft Sub Series | Period — the join key to
    the aircraft dimension. It carries Period on purpose: an aircraft's Agreed Value changes month to month,
    so the "dimension" is really a monthly state table and the key must include the month.
    Registration is deliberately NOT changed: it already encodes sub-series + short serial for an
    unregistered airframe ('A6-A320-251N neo-124349'), and folding Period into it would turn ONE aircraft
    into ~25 different ones, breaking both the Agreed-Value history (keyed on Registration) and every fleet
    count in the report.

 4. forecast.aircraft_information — the aircraft dimension: one row per (Registration, Aircraft Sub Series,
    Period), keyed by MERGED_KEY. Counting aircraft off this view needs DISTINCT "Registration".

Revision ID: forecast_report_dims
Revises: forecast_coeff_history_key
Create Date: 2026-07-10
"""
from alembic import op

revision = "forecast_report_dims"
down_revision = "forecast_coeff_history_key"
branch_labels = None
depends_on = None


def _city_country(city: str, country: str) -> str:
    """'Attock (Pakistan)'; degrades to whichever half exists, NULL if neither does."""
    return (f"""CASE WHEN nullif({city},'') IS NOT NULL AND nullif({country},'') IS NOT NULL
                     THEN {city} || ' (' || {country} || ')'
                     ELSE coalesce(nullif({city},''), nullif({country},'')) END""")


_ADD_COLS = f"""
ALTER TABLE forecast.acys_summary_by_day
    ADD COLUMN "Origin City&Country" text
        GENERATED ALWAYS AS ({_city_country('"Origin City"', '"Origin Country"')}) STORED,
    ADD COLUMN "Destination City&Country" text
        GENERATED ALWAYS AS ({_city_country('"Destination City"', '"Destination Country"')}) STORED
"""

# MERGED_KEY: Registration | Aircraft Sub Series | Period. All three are GROUP BY columns of the matview,
# so it can be computed in the SELECT list.
_MERGED_KEY = """"Registration" || '|' || coalesce("Aircraft Sub Series",'') || '|' || "Period\""""

# ── acys_summary_grouped (matview) ─────────────────────────────────────────────────────────────────────
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
    "Origin City&Country","Destination City&Country",
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

_GROUPED = f"""
CREATE MATERIALIZED VIEW forecast.acys_summary_grouped AS
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
    {_MERGED_KEY} AS "MERGED_KEY",
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

# ── acys_summary_grouped_by_reg (unchanged shape: still NO geography, hence no City&Country) ────────────
_BY_REG_KEYS = """    "Registration", "Period",
    "Operator", "Master Series", "Manufacturer", "Aircraft Sub Series", "Primary Usage",
    "Contract Year",
    "Agreed Value", "Total Seats", "Total PAX",
    "Delivery Date", "Lease Type", "Lease Dry Wet", "Operational Lessor",
    "Data Type",
    "Date\""""

_BY_REG = f"""
CREATE VIEW forecast.acys_summary_grouped_by_reg AS
SELECT
{_BY_REG_KEYS},
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
{_BY_REG_KEYS}
"""

# ── aircraft_information — the aircraft dimension, one row per (Registration, Sub Series, Period) ───────
# Sourced from acys_summary_grouped_by_reg (~4k rows), which is already per aircraft-month; the remaining
# split there is by Contract Year / Data Type (the anchor month straddles two CYs and both data types), so
# those collapse away here. Every attribute except Agreed Value is constant per aircraft-month, so max()
# simply picks that one value; Agreed Value is the month's value (the whole point of keying on Period).
_AIRCRAFT_INFO = f"""
CREATE VIEW forecast.aircraft_information AS
SELECT
    {_MERGED_KEY}             AS "MERGED_KEY",
    "Registration",
    "Period",
    "Aircraft Sub Series",
    max("Operator")           AS "Operator",
    max("Master Series")      AS "Master Series",
    max("Manufacturer")       AS "Manufacturer",
    max("Primary Usage")      AS "Primary Usage",
    max("Agreed Value")       AS "Agreed Value",
    max("Lease Type")         AS "Lease Type",
    max("Lease Dry Wet")      AS "Lease Dry Wet",
    max("Operational Lessor") AS "Operational Lessor"
FROM forecast.acys_summary_grouped_by_reg
GROUP BY "Registration", "Aircraft Sub Series", "Period"
"""

# ── powerbi.z_dates_acys — now also answers "which Contract Year is this day in?" ───────────────────────
# The anchor is the request date, recovered from the data: actuals stop at yesterday and the forecast starts
# today, so the EARLIEST forecast date IS the request date. Same day-precise rule as the forecast model:
# CY = year(d) - 1 when (month, day) <= (anchor month, anchor day), else year(d).
_Z_DATES_ACYS = """
CREATE VIEW powerbi.z_dates_acys AS
WITH b AS (
    SELECT date_trunc('month', min("Date"))::date                      AS lo,
           (date_trunc('month', max("Date")) + INTERVAL '1 month'
                                             - INTERVAL '1 day')::date AS hi
    FROM forecast.acys_summary_grouped
),
anchor AS (
    SELECT coalesce(
        (SELECT min("Date") FROM forecast.acys_summary_by_day WHERE "Data Type" = 'Forecast'),
        (SELECT max("Date") + 1 FROM forecast.acys_summary_by_day),
        DATE '2022-07-01'
    ) AS d
)
SELECT d.*,
       'CY' || (extract(year from d."Date")::int - CASE
           WHEN (extract(month from d."Date")::int, extract(day from d."Date")::int)
                <= (extract(month from a.d)::int, extract(day from a.d)::int)
           THEN 1 ELSE 0 END)::text AS "Contract Year"
FROM powerbi.z_dates d
CROSS JOIN b
CROSS JOIN anchor a
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
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['grp_aixii_read','grp_aviation_write'] LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('GRANT SELECT ON forecast.acys_summary_grouped, '
                     'forecast.acys_summary_grouped_by_reg, forecast.aircraft_information, '
                     'powerbi.z_dates_acys TO %I', r);
    END IF;
  END LOOP;
END $$;
"""

_INDEXES = [
    'CREATE INDEX ix_acys_grouped_operator ON forecast.acys_summary_grouped ("Operator")',
    'CREATE INDEX ix_acys_grouped_cy ON forecast.acys_summary_grouped ("Contract Year")',
    'CREATE INDEX ix_acys_grouped_reg ON forecast.acys_summary_grouped ("Registration")',
    'CREATE INDEX ix_acys_grouped_dtype ON forecast.acys_summary_grouped ("Data Type")',
    'CREATE INDEX ix_acys_grouped_date ON forecast.acys_summary_grouped ("Date")',
    'CREATE INDEX ix_acys_grouped_mkey ON forecast.acys_summary_grouped ("MERGED_KEY")',
    # the z_dates_acys anchor probe: min("Date") WHERE "Data Type" = 'Forecast'
    'CREATE INDEX IF NOT EXISTS ix_acys_by_day_dtype_date '
    'ON forecast.acys_summary_by_day ("Data Type", "Date")',
]


def _drop_chain() -> None:
    op.execute("DROP VIEW IF EXISTS powerbi.z_dates_acys")
    op.execute("DROP VIEW IF EXISTS forecast.aircraft_information")
    op.execute("DROP VIEW IF EXISTS forecast.acys_summary_grouped_by_reg")
    op.execute("DROP VIEW IF EXISTS forecast.z_contract_years")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS forecast.acys_summary_grouped")


def upgrade() -> None:
    _drop_chain()                     # z_contract_years goes away for good
    op.execute(_ADD_COLS)
    op.execute(_GROUPED)
    for ix in _INDEXES:
        op.execute(ix)
    op.execute(_OWNER)
    op.execute(_BY_REG)
    op.execute(_AIRCRAFT_INFO)
    op.execute(_Z_DATES_ACYS)
    op.execute(_GRANTS)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS powerbi.z_dates_acys")
    op.execute("DROP VIEW IF EXISTS forecast.aircraft_information")
    op.execute("DROP VIEW IF EXISTS forecast.acys_summary_grouped_by_reg")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS forecast.acys_summary_grouped")
    op.execute('ALTER TABLE forecast.acys_summary_by_day '
               'DROP COLUMN IF EXISTS "Origin City&Country", '
               'DROP COLUMN IF EXISTS "Destination City&Country"')
    op.execute('DROP INDEX IF EXISTS forecast.ix_acys_by_day_dtype_date')
    # rebuild the pre-change chain (no City&Country, no MERGED_KEY) + z_contract_years
    prev_cols = _GROUP_COLS.replace('\n    "Origin City&Country","Destination City&Country",', '')
    op.execute(_GROUPED.replace(f'    {_MERGED_KEY} AS "MERGED_KEY",\n', '')
                       .replace(_GROUP_COLS, prev_cols))
    for ix in _INDEXES[:5]:
        op.execute(ix)
    op.execute(_OWNER)
    op.execute(_BY_REG)
    op.execute("""
CREATE VIEW forecast.z_contract_years AS
SELECT DISTINCT "Contract Year" FROM forecast.acys_summary_grouped
WHERE "Contract Year" IS NOT NULL ORDER BY 1""")
    op.execute("""
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
  AND d."Date" <= coalesce(b.hi, DATE '2029-12-31')""")
    op.execute(_GRANTS.replace("forecast.aircraft_information, ", ""))
