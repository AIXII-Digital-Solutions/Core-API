"""acys_summary_grouped gains two columns:
  * "ROUTE_KEY"        = MERGED_KEY | DateInt | IATA Origin | IATA Destination  (a row-grain composite key:
                         aircraft-month + calendar-int + the route, on top of MERGED_KEY's aircraft-month).
  * "OD City&Country"  = "Origin City&Country" & "Destination City&Country"  ('Attock (Pakistan) & Doha
                         (Qatar)') — origin and destination geography in one label.

Both are computed from columns already in the matview's GROUP BY, so they add no new grain. The matview is
rebuilt (with the dependency chain z_dates_acys / grouped_by_reg / aircraft_information) — only
acys_summary_grouped carries the new columns; grouped_by_reg / aircraft_information are per-aircraft and drop
the route/geo dimensions, so they are recreated unchanged (latest definitions: grouped_by_reg WITH DateInt,
aircraft_information WITH the 'Not Leased' relabel).

The matview/view bodies are inlined verbatim from the current head (forecast_dateint_join grouped/by_reg +
forecast_aircraft_not_leased aircraft_information + zdates_cy_indata z_dates_acys) plus the two new columns.

Revision ID: forecast_grouped_route_cols
Revises: forecast_aircraft_not_leased
Create Date: 2026-07-10
"""
from alembic import op

revision = "forecast_grouped_route_cols"
down_revision = "forecast_aircraft_not_leased"
branch_labels = None
depends_on = None


def _dateint(col: str) -> str:
    return (f"(EXTRACT(year FROM {col})::int * 10000 "
            f"+ EXTRACT(month FROM {col})::int * 100 "
            f"+ EXTRACT(day FROM {col})::int)")


_MK = """"Registration" || '|' || coalesce("Aircraft Sub Series",'') || '|' || "Period\""""
_PERIOD_DATE = """to_date("Period", 'MM-YYYY')"""

# OD City&Country: "O (Country) & D (Country)"; concat_ws skips a NULL side, nullif drops the all-NULL case
_OD = """nullif(concat_ws(' & ', "Origin City&Country", "Destination City&Country"), '')"""

# City Route — an UNDIRECTED "City (Country)" pair for distance analysis: origin & destination
# "City (Country)" labels sorted, joined with ' - ', so a route and its reverse collapse to ONE value
# ("Sharjah (United Arab Emirates) - Karachi (Pakistan)" AND the reverse both become
# "Karachi (Pakistan) - Sharjah (United Arab Emirates)"). Uses City&Country (not bare City) so same-named
# cities in different countries stay distinct. NULL unless BOTH endpoints are known.
_CITY_ROUTE = ("""CASE WHEN nullif("Origin City&Country",'') IS NOT NULL
                    AND nullif("Destination City&Country",'') IS NOT NULL
               THEN LEAST("Origin City&Country","Destination City&Country") || ' - '
                    || GREATEST("Origin City&Country","Destination City&Country")
               ELSE NULL END""")

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

# ROUTE_KEY — a UNIQUE, refresh-STABLE row key. A readable prefix (MERGED_KEY | DateInt | IATA Origin | IATA
# Destination) is not enough on its own: the matview's grain is the FULL 32-column GROUP BY, so several rows
# can share those four fields (e.g. diversions differing only in "IATA Destination Actual"). Appending
# md5(ROW(<all GROUP BY columns>)::text) makes it unique — each matview row IS a distinct combination of the
# GROUP BY columns, so the hash of them is one-to-one with the row. It is content-derived, hence STABLE
# across refreshes (unlike row_number(), which would renumber and break PowerBI relationships). ROW(...)::text
# canonically encodes NULLs and quoting, so distinct rows never collide into the same hash input.
_ROUTE_KEY = (f"""{_MK} || '|' || ({_dateint(_PERIOD_DATE)})::text """
              """|| '|' || coalesce("IATA Origin",'') || '|' || coalesce("IATA Destination",'') """
              f"""|| '|' || md5(ROW({_GROUP_COLS})::text)""")

_AGG = """    min("Age")                AS "Age",
    count(*)                  AS "# Of Flights",
    sum("Circle Distance")    AS "Circle Distance",
    sum("Actual Distance FR") AS "Actual Distance FR",
    sum("Flight Time")        AS "Flight Time",
    sum("Flight Time FR")     AS "Flight Time FR" """

_DRY = "\"Date\" IS NOT NULL AND \"Lease Dry Wet\" IS DISTINCT FROM 'Wet' AND \"Agreed Value\" > 0"
_WAVG = ("((array_agg(av.v ORDER BY av.mon))[1] + "
         "(array_agg(av.v ORDER BY av.mon DESC))[1]) / 2.0")


def _grouped(route_cols: bool) -> str:
    routekey = f'    {_ROUTE_KEY} AS "ROUTE_KEY",\n' if route_cols else ""
    od = (f'    {_OD} AS "OD City&Country",\n'
          f'    {_CITY_ROUTE} AS "City Route",\n') if route_cols else ""
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
{routekey}    {_GROUP_COLS},
    {_PERIOD_DATE} AS "Date",
    {_dateint(_PERIOD_DATE)} AS "DateInt",
{od}{_AGG},
    max(cyv.inception) AS "Agreed Value on Inception",
    max(cyv.at_end)    AS "Agreed Value at the End of the Contract",
    max(cyv.wavg)      AS "Weighted Average Agreed Value",
    max(cyv.awavg)     AS "Activity-Weighted Average Agreed Value"
FROM forecast.acys_summary_by_day s
LEFT JOIN cyv ON cyv.reg = s."Registration" AND cyv.cy = s."Contract Year"
GROUP BY {_GROUP_COLS}
"""


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
    {_MK} AS "MERGED_KEY",
{_BY_REG_KEYS},
    {_dateint('"Date"')} AS "DateInt",
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


def _lease(col: str) -> str:
    return f"coalesce(nullif(max(\"{col}\"),''), 'Not Leased') AS \"{col}\""


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
    {_lease("Lease Type")},
    {_lease("Lease Dry Wet")},
    {_lease("Operational Lessor")}
FROM forecast.acys_summary_grouped_by_reg
GROUP BY "Registration", "Aircraft Sub Series", "Period"
"""

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
    -- The CY anchor DAY is as_of.day. The forecast horizon ends exactly on as_of + FORECAST_HORIZON_YEARS
    -- and the horizon month is prorated to as_of.day, so the overall max("Date") IS (as_of.month, as_of.day).
    -- Do NOT use the first forecast date: the forecast now starts at last_fact+1 (which can be EARLIER than
    -- as_of), so its month/day is not the CY anchor — only the horizon carries as_of.day.
    SELECT coalesce(
        (SELECT max("Date") FROM forecast.acys_summary_by_day),
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

_INDEXES = [
    'CREATE INDEX ix_acys_grouped_operator ON forecast.acys_summary_grouped ("Operator")',
    'CREATE INDEX ix_acys_grouped_cy ON forecast.acys_summary_grouped ("Contract Year")',
    'CREATE INDEX ix_acys_grouped_reg ON forecast.acys_summary_grouped ("Registration")',
    'CREATE INDEX ix_acys_grouped_dtype ON forecast.acys_summary_grouped ("Data Type")',
    'CREATE INDEX ix_acys_grouped_date ON forecast.acys_summary_grouped ("Date")',
    'CREATE INDEX ix_acys_grouped_mkey ON forecast.acys_summary_grouped ("MERGED_KEY")',
    'CREATE INDEX ix_acys_grouped_dateint ON forecast.acys_summary_grouped ("DateInt")',
]


def _drop_chain() -> None:
    op.execute("DROP VIEW IF EXISTS powerbi.z_dates_acys")
    op.execute("DROP VIEW IF EXISTS forecast.aircraft_information")
    op.execute("DROP VIEW IF EXISTS forecast.acys_summary_grouped_by_reg")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS forecast.acys_summary_grouped")


def _rebuild(route_cols: bool) -> None:
    op.execute(_grouped(route_cols))
    for ix in _INDEXES:
        op.execute(ix)
    op.execute(_OWNER)
    op.execute(_BY_REG)
    op.execute(_AIRCRAFT_INFO)
    op.execute(_Z_DATES_ACYS)
    op.execute(_GRANTS)


def upgrade() -> None:
    _drop_chain()
    _rebuild(route_cols=True)


def downgrade() -> None:
    _drop_chain()
    _rebuild(route_cols=False)
