"""acys_summary_grouped route/geo columns (this migration is the source-of-truth body for the whole
forecast view chain; it is edited in place and re-materialised via downgrade+upgrade).

acys_summary_grouped carries, on top of the GROUP-BY columns:
  * "ROUTE_KEY"        = MERGED_KEY | DateInt | IATA Origin | IATA Destination + md5(row) — a row-grain,
                         refresh-STABLE unique key.
  * "OD City&Country"  = "Origin City&Country" & "Destination City&Country"  ('Attock (Pakistan) & Doha
                         (Qatar)') — origin and destination geography in one label.
  * "City Pairs"       = UNDIRECTED "City (Country)" pair (sorted, reverse collapses). (renamed from the
                         former "City Route".)
  * "Country Pairs"    = UNDIRECTED origin/destination country pair (sorted; domestic -> single country name).
  * "Age Group"        = the aircraft's age bucketed into 10 number-prefixed bands (also on by_reg).
All are computed from columns already in the matview's GROUP BY, so they add no new grain.

The matview is rebuilt with its dependency chain (z_age_group / z_dates_acys / aircraft_information /
grouped_by_reg). aircraft_information additionally carries "Age Group" and "Current Family" — the latter read
from Cirium's current snapshot per registration (cross-schema join to cirium.ciriumaircrafts).

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

# The "Date" column is day-precise and aligned to the Contract-Year boundary. A Contract Year runs
# (anchor_day+1)-<anchor_month> .. anchor_day-<anchor_month> of the next year (e.g. CY2025 = 19-Aug-2025 ..
# 18-Aug-2026 for an 18-Aug anchor). So:
#   * a NON-anchor-month cell sits at `anchor_day + 1` of its month (the canonical within-CY day), and
#   * the ANCHOR month (= as_of.month, e.g. August) SPLITS into the two CYs that share it:
#       - the ENDING half  (rows whose CY = year(Period) - 1, i.e. days 1..anchor_day) -> `anchor_day`
#       - the STARTING half (rows whose CY = year(Period),   i.e. days anchor_day+1..EOM) -> `anchor_day + 1`
#     So at each CY boundary you get e.g. `CY2025 18/08/2026` AND `CY2026 19/08/2026`. The global forecast
#     horizon is just the terminal ENDING half (its anchor month has only the CY=year-1 rows), hence anchor_day.
# TWO higher-priority overrides handle the Actuals->Forecast seam, which falls mid-month (forecast starts at
# last_fact+1 = today, so the LAST actual day and FIRST forecast day sit inside the same month):
#   * the Actuals cell of the month that holds the last actual  -> that exact last-actual date  (e.g. 14/07/2026)
#   * the Forecast cell of the month that holds the first forecast -> that exact first-forecast date (15/07/2026)
# so the seam reads Actuals 14/07 / Forecast 15/07 instead of both collapsing onto the canonical 19/07.
# anchor_day/anchor_month/last_actual/first_forecast all come from acys_summary_by_day. Days are clamped to the
# month length so a large as_of.day never overflows make_date. "DateInt" AND "ROUTE_KEY" derive from this SAME
# day-precise date so all three agree AND each cell maps, via the DateInt calendar join, back onto its OWN CY in
# z_dates_acys (18-Aug -> CY2025, 19-Aug -> CY2026). by_reg's DateInt derives from grouped."Date", so it follows.
_ANCHOR_CTE = """anchor AS (
    SELECT extract(day from mx)::int AS anchor_day, extract(month from mx)::int AS anchor_month,
           la AS last_actual, ff AS first_forecast
    FROM (SELECT max("Date") AS mx,
                 max("Date") FILTER (WHERE "Data Type" = 'Actuals')  AS la,
                 min("Date") FILTER (WHERE "Data Type" = 'Forecast') AS ff
          FROM forecast.acys_summary_by_day) t
)"""


def _period_daily() -> str:
    pd = _PERIOD_DATE
    dim = f"extract(day from (date_trunc('month', {pd}) + interval '1 month' - interval '1 day'))::int"
    y, mo = f"extract(year from {pd})::int", f"extract(month from {pd})::int"
    # "Contract Year" is text 'CY2026'; its 4-digit year. The ENDING half of the anchor month has CY = y-1.
    cy_year = 'right("Contract Year", 4)::int'
    return (f"""CASE
                WHEN "Data Type" = 'Actuals'
                     AND date_trunc('month', {pd}) = date_trunc('month', a.last_actual)
                THEN a.last_actual
                WHEN "Data Type" = 'Forecast'
                     AND date_trunc('month', {pd}) = date_trunc('month', a.first_forecast)
                THEN a.first_forecast
                WHEN {mo} = a.anchor_month AND {cy_year} = {y} - 1
                THEN make_date({y}, {mo}, LEAST(a.anchor_day, {dim}))
                ELSE make_date({y}, {mo}, LEAST(a.anchor_day + 1, {dim})) END""")

# OD City&Country: "O (Country) & D (Country)"; concat_ws skips a NULL side, nullif drops the all-NULL case
_OD = """nullif(concat_ws(' & ', "Origin City&Country", "Destination City&Country"), '')"""

# City Pairs — an UNDIRECTED "City (Country)" pair for distance analysis: origin & destination
# "City (Country)" labels sorted, joined with ' - ', so a route and its reverse collapse to ONE value
# ("Sharjah (United Arab Emirates) - Karachi (Pakistan)" AND the reverse both become
# "Karachi (Pakistan) - Sharjah (United Arab Emirates)"). Uses City&Country (not bare City) so same-named
# cities in different countries stay distinct. NULL unless BOTH endpoints are known.
_CITY_ROUTE = ("""CASE WHEN nullif("Origin City&Country",'') IS NOT NULL
                    AND nullif("Destination City&Country",'') IS NOT NULL
               THEN LEAST("Origin City&Country","Destination City&Country") || ' - '
                    || GREATEST("Origin City&Country","Destination City&Country")
               ELSE NULL END""")

# Country Pairs — the COUNTRY-level analogue of City Pairs: an UNDIRECTED origin/destination country pair,
# sorted LEAST/GREATEST so a pair and its reverse are ONE value. A DOMESTIC flight (both endpoints in the same
# country) collapses to the single country name ("United Arab Emirates") rather than "X - X". NULL unless BOTH
# country endpoints are known.
_COUNTRY_ROUTE = ("""CASE
               WHEN nullif("Origin Country",'') IS NULL OR nullif("Destination Country",'') IS NULL THEN NULL
               WHEN "Origin Country" = "Destination Country" THEN "Origin Country"
               ELSE LEAST("Origin Country","Destination Country") || ' - '
                    || GREATEST("Origin Country","Destination Country") END""")

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
_ROUTE_KEY = (f"""{_MK} || '|' || ({_dateint(_period_daily())})::text """
              """|| '|' || coalesce("IATA Origin",'') || '|' || coalesce("IATA Destination",'') """
              f"""|| '|' || md5(ROW({_GROUP_COLS})::text)""")

_AGG = """    min("Age")                AS "Age",
    count(s."Date")           AS "# Of Flights",
    sum("Circle Distance")    AS "Circle Distance",
    sum("Actual Distance FR") AS "Actual Distance FR",
    sum("Flight Time")        AS "Flight Time",
    sum("Flight Time FR")     AS "Flight Time FR" """

_DRY = "\"Date\" IS NOT NULL AND \"Lease Dry Wet\" IS DISTINCT FROM 'Wet' AND \"Agreed Value\" > 0"
_WAVG = ("((array_agg(av.v ORDER BY av.mon))[1] + "
         "(array_agg(av.v ORDER BY av.mon DESC))[1]) / 2.0")


def _age_group(delivery: str = '"Delivery Date"') -> str:
    # "Age Group" — the aircraft's age (years) bucketed into fixed bands. The number prefix ('1. …') is part of
    # the label ON PURPOSE so PowerBI sorts the bands correctly. Age = the min flight "Age" of the bucket, else
    # (a flightless fleet-presence stub) from the delivery date to the bucket's month; NULL delivery -> NULL.
    # `delivery` is the delivery-date expression (a group column in the matview/by_reg, an aggregate elsewhere).
    age = (f'coalesce(min("Age"), CASE WHEN {delivery} IS NOT NULL '
           f'THEN GREATEST(0, ({_PERIOD_DATE} - {delivery})::numeric / 365.25) END)')
    return f"""CASE
        WHEN ({age}) IS NULL THEN NULL
        WHEN ({age}) < 1  THEN '1. Less than one year'
        WHEN ({age}) < 2  THEN '2. From 1 to 2 years'
        WHEN ({age}) < 4  THEN '3. From 2 to 4 years'
        WHEN ({age}) < 6  THEN '4. From 4 to 6 years'
        WHEN ({age}) < 8  THEN '5. From 6 to 8 years'
        WHEN ({age}) < 10 THEN '6. From 8 to 10 years'
        WHEN ({age}) < 12 THEN '7. From 10 to 12 years'
        WHEN ({age}) < 14 THEN '8. From 12 to 14 years'
        WHEN ({age}) < 16 THEN '9. From 14 to 16 years'
        ELSE '10. More than 16 years' END"""


def _age_group_sort(delivery: str = '"Delivery Date"') -> str:
    # PowerBI's sort-by-column key for "Age Group": the numeric band 1..10 parsed from the "N. " prefix. The
    # prefix alone does NOT sort right (PowerBI sorts the labels as TEXT, so "10. …" lands between "1." and
    # "2."). Emitted next to every "Age Group" column. NULL age group -> NULL sort.
    return f"split_part({_age_group(delivery)}, '.', 1)::int"


def _grouped(route_cols: bool) -> str:
    routekey = f'    {_ROUTE_KEY} AS "ROUTE_KEY",\n' if route_cols else ""
    od = (f'    {_OD} AS "OD City&Country",\n'
          f'    {_CITY_ROUTE} AS "City Pairs",\n'
          f'    {_COUNTRY_ROUTE} AS "Country Pairs",\n') if route_cols else ""
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
),
{_ANCHOR_CTE}
SELECT
    {_MK} AS "MERGED_KEY",
{routekey}    {_GROUP_COLS},
    {_period_daily()} AS "Date",
    {_dateint(_period_daily())} AS "DateInt",
{od}{_AGG},
    max(cyv.inception) AS "Agreed Value on Inception",
    max(cyv.at_end)    AS "Agreed Value at the End of the Contract",
    max(cyv.wavg)      AS "Weighted Average Agreed Value",
    max(cyv.awavg)     AS "Activity-Weighted Average Agreed Value",
    {_age_group()} AS "Age Group",
    {_age_group_sort()} AS "Age Group Sort"
FROM forecast.acys_summary_by_day s
LEFT JOIN cyv ON cyv.reg = s."Registration" AND cyv.cy = s."Contract Year"
CROSS JOIN anchor a
GROUP BY {_GROUP_COLS}, a.anchor_day, a.anchor_month, a.last_actual, a.first_forecast
"""


_BY_REG_KEYS = """    "Registration", "Period",
    "Operator", "Master Series", "Manufacturer", "Aircraft Sub Series", "Primary Usage",
    "Contract Year",
    "Agreed Value", "Total Seats", "Total PAX",
    "Delivery Date", "Lease Type", "Lease Dry Wet", "Operational Lessor",
    "Data Type",
    "Date\""""

_BY_REG = f"""
CREATE MATERIALIZED VIEW forecast.acys_summary_grouped_by_reg AS
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
    max("Activity-Weighted Average Agreed Value")  AS "Activity-Weighted Average Agreed Value",
    {_age_group()}                                 AS "Age Group",
    {_age_group_sort()}                            AS "Age Group Sort"
FROM forecast.acys_summary_grouped
GROUP BY
{_BY_REG_KEYS}
"""


def _lease(col: str) -> str:
    return f"coalesce(nullif(max(\"{col}\"),''), 'Not Leased') AS \"{col}\""


# aircraft_information also carries "Current Family" — the aircraft's CURRENT family (e.g. 'A320 Family'),
# read from Cirium's newest snapshot per registration: the latest revision of EACH plan_type (Commercial +
# Business&Helicopters) and then, per tail, the newest (revision_id, id). Joined on Registration (family is a
# per-tail current attribute, not per-period). Wet-lease / carry-forward tails absent from the current Cirium
# roster get NULL — the same tails that already have NULL for the other 'current' Cirium attributes.
_AIRCRAFT_INFO = f"""
CREATE MATERIALIZED VIEW forecast.aircraft_information AS
WITH latest_rev AS (
    SELECT DISTINCT ON (plan_type) id FROM cirium.aircraftrevision
    ORDER BY plan_type, to_date(period,'MM-YYYY') DESC, id DESC
),
cur_family AS (
    SELECT DISTINCT ON (ca."Registration") ca."Registration" AS reg, ca."Current Family" AS cf
    FROM cirium.ciriumaircrafts ca JOIN latest_rev lr ON lr.id = ca.revision_id
    ORDER BY ca."Registration", ca.revision_id DESC, ca.id DESC
)
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
    {_lease("Operational Lessor")},
    max(cf.cf)                AS "Current Family",
    {_age_group('max("Delivery Date")')} AS "Age Group",
    {_age_group_sort('max("Delivery Date")')} AS "Age Group Sort"
FROM forecast.acys_summary_grouped_by_reg
LEFT JOIN cur_family cf ON cf.reg = "Registration"
GROUP BY "Registration", "Aircraft Sub Series", "Period"
"""

# powerbi.z_age_group — a static dimension of the 10 Age-Group bands (a PowerBI slicer / relationship target).
# The column is named exactly "Age Group" to match the fact tables. The "N. " label prefix ALONE does NOT sort
# right in PowerBI — it sorts the labels as TEXT, so "10. …" lands between "1. …" and "2. …". So we expose a
# numeric "Age Group Sort" (1..10, parsed from the prefix) and PowerBI must be told to Sort "Age Group" BY
# "Age Group Sort" (same trick as z_dates_acys."MonthSortInContractYear"). The view is also ORDER BY-ed so a
# plain SQL read returns 1..10, but PowerBI ignores source order — the sort-by-column setting is what matters.
_Z_AGE_GROUP = """
CREATE VIEW powerbi.z_age_group AS
SELECT v."Age Group",
       split_part(v."Age Group", '.', 1)::int AS "Age Group Sort"
FROM (VALUES
    ('1. Less than one year'),
    ('2. From 1 to 2 years'),
    ('3. From 2 to 4 years'),
    ('4. From 4 to 6 years'),
    ('5. From 6 to 8 years'),
    ('6. From 8 to 10 years'),
    ('7. From 10 to 12 years'),
    ('8. From 12 to 14 years'),
    ('9. From 14 to 16 years'),
    ('10. More than 16 years')
) AS v("Age Group")
ORDER BY 2
"""

_CY = """'CY' || (extract(year from d."Date")::int - CASE
             WHEN (extract(month from d."Date")::int, extract(day from d."Date")::int)
                  <= (extract(month from a.d)::int, extract(day from a.d)::int)
             THEN 1 ELSE 0 END)::text"""

# "MonthSortInContractYear" — the month's ORDINAL inside the Contract Year, 1..12, so a report can order the
# months the way the contract runs instead of Jan..Dec. The CY opens in the anchor month (= as_of's month), so
# with a September anchor: Sep -> 1, Oct -> 2, Nov -> 3, ... Aug -> 12. Pure month arithmetic modulo 12.
# NB the CY boundary is DAY-precise, so the anchor month itself appears at BOTH ends of a Contract Year (e.g.
# CY2025 = 18-Sep-2025 .. 17-Sep-2026) and both halves get ordinal 1 — which is what "September is the first
# month of the contract year" means. Slice by "Contract Year" as well and the two halves never mix.
_MONTH_SORT_IN_CY = """((extract(month from d."Date")::int
                         - extract(month from a.d)::int + 12) % 12) + 1"""

_Z_DATES_ACYS = f"""
CREATE MATERIALIZED VIEW powerbi.z_dates_acys AS
WITH anchor AS (
    -- The CY anchor DAY is as_of.day. The forecast horizon ends exactly on as_of + FORECAST_HORIZON_YEARS
    -- and the horizon month is prorated to as_of.day, so the overall max("Date") IS (as_of.month, as_of.day).
    -- Do NOT use the first forecast date: the forecast now starts at last_fact+1 (which can be EARLIER than
    -- as_of), so its month/day is not the CY anchor — only the horizon carries as_of.day.
    SELECT coalesce(
        (SELECT max("Date") FROM forecast.acys_summary_by_day),
        DATE '2022-07-01'
    ) AS d
),
-- CONTRACT-YEAR-ALIGNED WINDOW. The dates table must start and end on a CONTRACT-YEAR BOUNDARY, not on the
-- raw data range. The old lower bound (month-start of the earliest fact = 2022-07-01) sits in the MIDDLE of a
-- contract year, so the sub-boundary head labels as the PRIOR contract year — a spurious CY2021 with no full
-- year of data behind it. (It also read acys_summary_grouped, which carries stray 2003-era stub dates.)
--   lo = the first CY boundary AT/AFTER the history floor (2022-07-01). A CY opens the day AFTER the anchor
--        day, so with a 17-Sep anchor the window starts 18-Sep of the first data year — dropping the partial
--        leading year, which is exactly what "no CY2021" means. (Verified: zero DATED facts sit below this
--        boundary, so only empty calendar dates are trimmed.)
--   hi = the anchor itself — the horizon end, which IS a CY boundary (as_of.day) — so there is no trailing
--        partial year (previously the month-end overshoot produced a NULL-CY tail).
-- Feb-29 anchor is clamped to the 28th (the fixed 2022/2023 target years are non-leap), matching _cy2022_floor.
p AS (
    SELECT a.d,
           extract(month from a.d)::int AS am,
           CASE WHEN extract(month from a.d)::int = 2 AND extract(day from a.d)::int > 28
                THEN 28 ELSE extract(day from a.d)::int END AS ad,
           extract(year from DATE '2022-07-01')::int AS hy
    FROM anchor a
),
b AS (
    SELECT CASE WHEN make_date(hy, am, ad) + 1 >= DATE '2022-07-01'
                THEN make_date(hy, am, ad) + 1
                ELSE make_date(hy + 1, am, ad) + 1 END AS lo,
           d AS hi
    FROM p
),
cys AS (
    SELECT DISTINCT "Contract Year" cy
    FROM forecast.acys_summary_by_day
    WHERE "Contract Year" IS NOT NULL
),
-- The Actuals/Forecast split is a single cut DATE: the forecast starts the day AFTER the last actual, so
-- actuals and forecast never share a day and `max(Actuals date)` labels the whole daily calendar. A date is
-- 'Actuals' iff it is on/before that cut, else 'Forecast' — the gap day (last_fact+1, if the monthly-dated
-- forecast's first row lands later) falls to Forecast, which is right since the forecast horizon opens there.
-- coalesce keeps an actuals-less table (before the first run) from labelling everything 'Actuals'.
split AS (
    SELECT coalesce(max("Date"), DATE '2022-07-01') AS actual_hi
    FROM forecast.acys_summary_by_day
    WHERE "Data Type" = 'Actuals' AND "Date" IS NOT NULL
)
SELECT d.*, cys.cy AS "Contract Year",
       {_MONTH_SORT_IN_CY} AS "MonthSortInContractYear",
       CASE WHEN d."Date" <= s.actual_hi THEN 'Actuals' ELSE 'Forecast' END AS "Data Type"
FROM powerbi.z_dates d
CROSS JOIN b
CROSS JOIN anchor a
CROSS JOIN split s
LEFT JOIN cys ON cys.cy = {_CY}
WHERE d."Date" >= b.lo
  AND d."Date" <= b.hi
"""

# All four matviews must be OWNED by grp_aviation_write: external-worker REFRESHes every one at the end of a
# panel run (see panel.py), and only an owner (or a member of the owning role) may REFRESH.
_OWNER = """
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aviation_write') THEN
    EXECUTE 'ALTER MATERIALIZED VIEW forecast.acys_summary_grouped        OWNER TO grp_aviation_write';
    EXECUTE 'ALTER MATERIALIZED VIEW forecast.acys_summary_grouped_by_reg OWNER TO grp_aviation_write';
    EXECUTE 'ALTER MATERIALIZED VIEW forecast.aircraft_information        OWNER TO grp_aviation_write';
    EXECUTE 'ALTER MATERIALIZED VIEW powerbi.z_dates_acys                 OWNER TO grp_aviation_write';
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
                     'powerbi.z_dates_acys, powerbi.z_age_group TO %I', r);
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
    'CREATE INDEX ix_acys_grouped_agegroup ON forecast.acys_summary_grouped ("Age Group")',
]

# Indexes over the route/geo-only columns — created ONLY when route_cols=True (the upgrade path); the
# route_cols=False downgrade matview does not carry these columns.
#
# THIS LIST IS THE SOURCE OF TRUTH for the matview's geo indexes: DROP MATERIALIZED VIEW takes its indexes
# with it, so anything not listed here silently disappears the next time the chain is rebuilt. (The one-off
# migration `forecast_geo_indexes` creates the same set on an ALREADY-BUILT matview so no rebuild was needed
# to get them live — keep the two in sync.)
#
# Only columns worth FILTERING on are indexed. Evidence (pg_stat_user_indexes over 19 days of live use):
# ix_acys_grouped_citypairs = 34,429 scans, so PowerBI really does filter server-side here. Deliberately NOT
# indexed: "Operator" / "Manufacturer" / "Primary Usage" (distinct = 1 — an index can never help), and the
# ICAO trio (on flightradar.flightsummary the ICAO columns score 0 scans against 291,700 for IATA — nothing
# filters on ICAO).
_ROUTE_INDEXES = [
    'CREATE INDEX ix_acys_grouped_citypairs ON forecast.acys_summary_grouped ("City Pairs")',
    'CREATE INDEX ix_acys_grouped_countrypairs ON forecast.acys_summary_grouped ("Country Pairs")',
    # origin & destination geography — the "OD City&Country" label plus each side on its own
    'CREATE INDEX ix_acys_grouped_od ON forecast.acys_summary_grouped ("OD City&Country")',
    'CREATE INDEX ix_acys_grouped_o_citycountry ON forecast.acys_summary_grouped ("Origin City&Country")',
    'CREATE INDEX ix_acys_grouped_d_citycountry ON forecast.acys_summary_grouped ("Destination City&Country")',
    'CREATE INDEX ix_acys_grouped_o_country ON forecast.acys_summary_grouped ("Origin Country")',
    'CREATE INDEX ix_acys_grouped_d_country ON forecast.acys_summary_grouped ("Destination Country")',
    'CREATE INDEX ix_acys_grouped_o_city ON forecast.acys_summary_grouped ("Origin City")',
    'CREATE INDEX ix_acys_grouped_d_city ON forecast.acys_summary_grouped ("Destination City")',
    'CREATE INDEX ix_acys_grouped_o_airport ON forecast.acys_summary_grouped ("Origin Airport Name")',
    'CREATE INDEX ix_acys_grouped_d_airport ON forecast.acys_summary_grouped ("Destination Airport Name")',
    'CREATE INDEX ix_acys_grouped_iata_o ON forecast.acys_summary_grouped ("IATA Origin")',
    'CREATE INDEX ix_acys_grouped_iata_d ON forecast.acys_summary_grouped ("IATA Destination")',
    'CREATE INDEX ix_acys_grouped_iata_da ON forecast.acys_summary_grouped ("IATA Destination Actual")',
]


# Non-unique indexes on the by_reg / aircraft_information / z_dates_acys matviews — the columns PowerBI joins
# and slices on. DROP MATERIALIZED VIEW takes its indexes with it, so this list is the source of truth for
# them (same rule as _ROUTE_INDEXES). Plain (not unique) — REFRESH is non-CONCURRENT (see panel.py), so no
# unique key is required, and by_reg has no small natural unique key anyway (its grain is the full GROUP BY).
_BY_REG_INDEXES = [
    'CREATE INDEX ix_by_reg_mkey     ON forecast.acys_summary_grouped_by_reg ("MERGED_KEY")',
    'CREATE INDEX ix_by_reg_reg      ON forecast.acys_summary_grouped_by_reg ("Registration")',
    'CREATE INDEX ix_by_reg_cy       ON forecast.acys_summary_grouped_by_reg ("Contract Year")',
    'CREATE INDEX ix_by_reg_dtype    ON forecast.acys_summary_grouped_by_reg ("Data Type")',
    'CREATE INDEX ix_by_reg_dateint  ON forecast.acys_summary_grouped_by_reg ("DateInt")',
]
_AIRCRAFT_INFO_INDEXES = [
    'CREATE INDEX ix_acinfo_mkey     ON forecast.aircraft_information ("MERGED_KEY")',
    'CREATE INDEX ix_acinfo_reg      ON forecast.aircraft_information ("Registration")',
    'CREATE INDEX ix_acinfo_agegroup ON forecast.aircraft_information ("Age Group")',
    'CREATE INDEX ix_acinfo_family   ON forecast.aircraft_information ("Current Family")',
]
_Z_DATES_INDEXES = [
    'CREATE INDEX ix_zdates_date  ON powerbi.z_dates_acys ("Date")',
    'CREATE INDEX ix_zdates_cy    ON powerbi.z_dates_acys ("Contract Year")',
    'CREATE INDEX ix_zdates_dtype ON powerbi.z_dates_acys ("Data Type")',
]


def _drop_chain() -> None:
    op.execute("DROP VIEW IF EXISTS powerbi.z_age_group")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS powerbi.z_dates_acys")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS forecast.aircraft_information")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS forecast.acys_summary_grouped_by_reg")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS forecast.acys_summary_grouped")


def _rebuild(route_cols: bool) -> None:
    # Built in dependency order, each matview WITH DATA reading the one it sits on:
    # acys_summary_by_day (table) -> grouped -> grouped_by_reg -> aircraft_information; z_dates_acys reads
    # acys_summary_by_day directly. Every panel run REFRESHes all four in this same order (panel.py).
    op.execute(_grouped(route_cols))
    for ix in _INDEXES:
        op.execute(ix)
    if route_cols:
        for ix in _ROUTE_INDEXES:
            op.execute(ix)
    op.execute(_BY_REG)
    for ix in _BY_REG_INDEXES:
        op.execute(ix)
    op.execute(_AIRCRAFT_INFO)
    for ix in _AIRCRAFT_INFO_INDEXES:
        op.execute(ix)
    op.execute(_Z_DATES_ACYS)
    for ix in _Z_DATES_INDEXES:
        op.execute(ix)
    op.execute(_Z_AGE_GROUP)
    op.execute(_OWNER)
    op.execute(_GRANTS)


def upgrade() -> None:
    _drop_chain()
    _rebuild(route_cols=True)


def downgrade() -> None:
    _drop_chain()
    _rebuild(route_cols=False)
