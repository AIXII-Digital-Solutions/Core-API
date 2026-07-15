"""DateInt (yyyymmdd integer) on every summary fact, so PowerBI can join facts→calendar on an INTEGER key
that always folds in DirectQuery — instead of on "Date", which cannot.

WHY NOT the "Date"::date cast that was proposed: "Date" is ALREADY the Postgres type `date` in all three
summary objects (verified: information_schema + pg_typeof both say `date`, values carry no time). Casting
an already-date column is a no-op. PBI still shows it as datetime2 because the PostgreSQL connector (Npgsql)
maps PG `date` → .NET DateTime regardless of the SQL, and a timestamp-vs-date fold is what DirectQuery's
connector refuses ("Specified method is not supported"). The cast cannot change the mapping.

The connector-agnostic fix is an INTEGER date key. `int = int` folds unconditionally, no date semantics
involved. powerbi.z_dates / z_dates_acys already expose "DateInt" (yyyymmdd); this adds the matching column
to the facts, and the model joins DateInt→DateInt.

  * acys_summary_by_day (table)        -> "DateInt" as a GENERATED ... STORED column: the 418k existing rows
        fill on add, every future insert fills itself, the worker needs no change (a generated column can't
        appear in the merge INSERT's column list anyway).
        Uses the ARITHMETIC form (year*10000 + month*100 + day), NOT to_char: to_char is only STABLE (it
        depends on lc_time/DateStyle) and Postgres rejects a non-IMMUTABLE generation expression. The
        arithmetic form is immutable and numerically identical to the calendar's to_char('YYYYMMDD').
  * acys_summary_grouped (matview)     -> "DateInt" from its "Date" (= 1st of the "Period" month), so a
        month rollup joins the calendar's first-of-month day exactly as the current Date→Date join already
        does. Needs the dependency-chain rebuild (z_dates_acys / grouped_by_reg / aircraft_information).
  * acys_summary_grouped_by_reg (view) -> "DateInt" from its carried "Date" (also first-of-month).

DateInt is placed right after "Date" in each. The matview/view bodies are the CURRENT definitions
(report_dims grouped + merged_key_more by_reg/aircraft_information + zdates_cy_indata z_dates_acys), copied
verbatim with only "DateInt" added — a migration is a snapshot, so they are inlined rather than imported.

Revision ID: forecast_dateint_join
Revises: forecast_merged_key_more
Create Date: 2026-07-10
"""
from alembic import op

revision = "forecast_dateint_join"
down_revision = "forecast_merged_key_more"
branch_labels = None
depends_on = None


def _dateint(col: str) -> str:
    """yyyymmdd integer from a date expression — IMMUTABLE (safe for a generated column), numerically
    identical to the calendar's to_char(d,'YYYYMMDD')::int."""
    return (f"(EXTRACT(year FROM {col})::int * 10000 "
            f"+ EXTRACT(month FROM {col})::int * 100 "
            f"+ EXTRACT(day FROM {col})::int)")


_MK = """"Registration" || '|' || coalesce("Aircraft Sub Series",'') || '|' || "Period\""""
_BYDAY_DATEINT = _dateint('"Date"')

# ── acys_summary_grouped (matview) — report_dims body, DateInt optional ─────────────────────────────────
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


def _grouped(dateint: bool) -> str:
    di = (f'    {_dateint("""to_date("Period", \'MM-YYYY\')""")} AS "DateInt",\n' if dateint else "")
    return f"""
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
    {_MK} AS "MERGED_KEY",
    {_GROUP_COLS},
    to_date("Period", 'MM-YYYY') AS "Date",
{di}{_AGG},
    max(cyv.inception) AS "Agreed Value on Inception",
    max(cyv.at_end)    AS "Agreed Value at the End of the Contract",
    max(cyv.wavg)      AS "Weighted Average Agreed Value",
    max(cyv.awavg)     AS "Activity-Weighted Average Agreed Value"
FROM forecast.acys_summary_by_day s
LEFT JOIN cyv ON cyv.reg = s."Registration" AND cyv.cy = s."Contract Year"
GROUP BY {_GROUP_COLS}
"""


# ── acys_summary_grouped_by_reg (view) — merged_key_more body, DateInt optional ─────────────────────────
_BY_REG_KEYS = """    "Registration", "Period",
    "Operator", "Master Series", "Manufacturer", "Aircraft Sub Series", "Primary Usage",
    "Contract Year",
    "Agreed Value", "Total Seats", "Total PAX",
    "Delivery Date", "Lease Type", "Lease Dry Wet", "Operational Lessor",
    "Data Type",
    "Date\""""


def _by_reg(dateint: bool) -> str:
    di = f'    {_BYDAY_DATEINT} AS "DateInt",\n' if dateint else ""
    return f"""
CREATE VIEW forecast.acys_summary_grouped_by_reg AS
SELECT
    {_MK} AS "MERGED_KEY",
{_BY_REG_KEYS},
{di}    min("Age")                                     AS "Age",
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


# ── aircraft_information (view) — unchanged, recreated because it depends on grouped_by_reg ─────────────
_AIRCRAFT_INFO = f"""
CREATE VIEW forecast.aircraft_information AS
SELECT
    {_MK}                     AS "MERGED_KEY",
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

# ── powerbi.z_dates_acys (view) — zdates_cy_indata body, unchanged, recreated (depends on grouped) ──────
_CY = """'CY' || (extract(year from d."Date")::int - CASE
             WHEN (extract(month from d."Date")::int, extract(day from d."Date")::int)
                  <= (extract(month from a.d)::int, extract(day from a.d)::int)
             THEN 1 ELSE 0 END)::text"""

_Z_DATES_ACYS = f"""
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
),
cys AS (
    SELECT DISTINCT "Contract Year" cy
    FROM forecast.acys_summary_by_day
    WHERE "Contract Year" IS NOT NULL
)
SELECT d.*, cys.cy AS "Contract Year"
FROM powerbi.z_dates d
CROSS JOIN b
CROSS JOIN anchor a
LEFT JOIN cys ON cys.cy = {_CY}
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

_BASE_INDEXES = [
    'CREATE INDEX ix_acys_grouped_operator ON forecast.acys_summary_grouped ("Operator")',
    'CREATE INDEX ix_acys_grouped_cy ON forecast.acys_summary_grouped ("Contract Year")',
    'CREATE INDEX ix_acys_grouped_reg ON forecast.acys_summary_grouped ("Registration")',
    'CREATE INDEX ix_acys_grouped_dtype ON forecast.acys_summary_grouped ("Data Type")',
    'CREATE INDEX ix_acys_grouped_date ON forecast.acys_summary_grouped ("Date")',
    'CREATE INDEX ix_acys_grouped_mkey ON forecast.acys_summary_grouped ("MERGED_KEY")',
]
_DATEINT_INDEX = 'CREATE INDEX ix_acys_grouped_dateint ON forecast.acys_summary_grouped ("DateInt")'


def _drop_chain() -> None:
    op.execute("DROP VIEW IF EXISTS powerbi.z_dates_acys")
    op.execute("DROP VIEW IF EXISTS forecast.aircraft_information")
    op.execute("DROP VIEW IF EXISTS forecast.acys_summary_grouped_by_reg")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS forecast.acys_summary_grouped")


def _rebuild(dateint: bool) -> None:
    op.execute(_grouped(dateint))
    for ix in _BASE_INDEXES:
        op.execute(ix)
    if dateint:
        op.execute(_DATEINT_INDEX)
    op.execute(_OWNER)
    op.execute(_by_reg(dateint))
    op.execute(_AIRCRAFT_INFO)
    op.execute(_Z_DATES_ACYS)
    op.execute(_GRANTS)


def upgrade() -> None:
    op.execute(f'ALTER TABLE forecast.acys_summary_by_day '
               f'ADD COLUMN "DateInt" integer GENERATED ALWAYS AS ({_BYDAY_DATEINT}) STORED')
    op.execute('CREATE INDEX ix_acys_by_day_dateint ON forecast.acys_summary_by_day ("DateInt")')
    _drop_chain()
    _rebuild(dateint=True)


def downgrade() -> None:
    _drop_chain()
    _rebuild(dateint=False)
    op.execute('DROP INDEX IF EXISTS forecast.ix_acys_by_day_dateint')
    op.execute('ALTER TABLE forecast.acys_summary_by_day DROP COLUMN IF EXISTS "DateInt"')
