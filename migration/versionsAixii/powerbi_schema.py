"""New `powerbi` schema — the home for PBI-only disconnected lookup tables (no aviation facts live here).

  * powerbi.z_top_n                    — "Value" (1..7) -> "Select TopN Countries/Cities to display" (3,5,10,15,20,30,40)
  * powerbi.z_display_by_city_country  — "Display Chart by:" ('By Country' / 'By City')
  * powerbi.z_dates                    — the calendar dimension, 01-Jul-2022 … 31-Dec-2029

The first two MOVE here from `forecast` (same definitions, lowercase names) and are dropped from `forecast`.
`forecast.z_contract_years` stays in `forecast` — it is derived from the facts, not a constant.

z_dates conventions (chosen with the user):
  * Weeks run **Sunday → Saturday** (Power Query default): DayInWeek 0=Sun … 6=Sat, WeekEnding = that week's
    Saturday, Week Number counts Sunday-start weeks with week 1 = the week containing Jan 1.
  * **FY starts in July**: FY2023 = Jul-2022 … Jun-2023 (matches the data anchor — history starts 01-Jul-2022).
  * to_char's 'Day'/'Month'/'Mon' patterns are English regardless of lc_time (the TM prefix would localize them),
    so the names are stable across servers.

A view over generate_series is always "already filled" — no storage, no refresh, no seed step.

Revision ID: powerbi_schema
Revises: forecast_z_contract_years
Create Date: 2026-07-10
"""
from alembic import op

revision = "powerbi_schema"
down_revision = "forecast_z_contract_years"
branch_labels = None
depends_on = None

_TOP_N = """
CREATE VIEW powerbi.z_top_n AS
SELECT * FROM (VALUES (1, 3), (2, 5), (3, 10), (4, 15), (5, 20), (6, 30), (7, 40))
    AS t("Value", "Select TopN Countries/Cities to display")
"""

_DISPLAY = """
CREATE VIEW powerbi.z_display_by_city_country AS
SELECT * FROM (VALUES ('By Country'), ('By City')) AS t("Display Chart by:")
"""

_DATES = """
CREATE VIEW powerbi.z_dates AS
SELECT
    d                                                            AS "Date",
    extract(year  from d)::int                                   AS "Calendar Year",
    to_char(d, 'YYYYMMDD')::int                                  AS "DateInt",          -- 20220701
    extract(dow   from d)::int                                   AS "DayInWeek",        -- 0=Sun … 6=Sat
    extract(day   from d)::int                                   AS "DayOfMonth",
    trim(to_char(d, 'Day'))                                      AS "DayOfWeekName",    -- Friday
    'FY' || (extract(year from d)::int
             + CASE WHEN extract(month from d)::int >= 7 THEN 1 ELSE 0 END)::text
                                                                 AS "FY",               -- Jul-2022 -> FY2023
    to_char(d, 'Mon YYYY')                                       AS "MonthInCalendar",  -- Jul 2022
    trim(to_char(d, 'Month'))                                    AS "MonthName",        -- July
    to_char(d, 'YYYYMM')::int                                    AS "MonthnYear",       -- 202207
    extract(month from d)::int                                   AS "MonthOfYear",
    'Qtr ' || extract(quarter from d)::int
            || ' '  || extract(year from d)::int                 AS "QuarterInCalendar",-- Qtr 3 2022
    extract(year from d)::int * 10
            + extract(quarter from d)::int                       AS "QuarternYear",     -- 20223
    extract(quarter from d)::int                                 AS "QuarterOfYear",
    to_char(d, 'YY')                                             AS "ShortYear",        -- 22
    (floor((extract(doy from d)::int
            + extract(dow from make_date(extract(year from d)::int, 1, 1))::int - 1) / 7.0)
     + 1)::int                                                   AS "Week Number",
    to_char(d + (6 - extract(dow from d)::int), 'YYYY-MM-DD')    AS "WeekEnding"        -- that week's Saturday
FROM (SELECT gs::date AS d
      FROM generate_series(DATE '2022-07-01', DATE '2029-12-31', INTERVAL '1 day') gs) t
"""

_GRANTS = """
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aixii_read') THEN
    GRANT USAGE ON SCHEMA powerbi TO grp_aixii_read;
    GRANT SELECT ON ALL TABLES IN SCHEMA powerbi TO grp_aixii_read;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aviation_write') THEN
    GRANT USAGE ON SCHEMA powerbi TO grp_aviation_write;
    GRANT SELECT ON ALL TABLES IN SCHEMA powerbi TO grp_aviation_write;
  END IF;
END $$;
"""

# recreate the two constant views back in `forecast` on downgrade (their original quoted names)
_FORECAST_TOP_N = """
CREATE VIEW forecast."Z_Top_N" AS
SELECT * FROM (VALUES (1, 3), (2, 5), (3, 10), (4, 15), (5, 20), (6, 30), (7, 40))
    AS t("Value", "Select TopN Countries/Cities to display")
"""

_FORECAST_DISPLAY = """
CREATE VIEW forecast."Z_Display_by_city_country" AS
SELECT * FROM (VALUES ('By Country'), ('By City')) AS t("Display Chart by:")
"""

_FORECAST_GRANTS = """
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aixii_read') THEN
    GRANT SELECT ON forecast."Z_Top_N", forecast."Z_Display_by_city_country" TO grp_aixii_read;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aviation_write') THEN
    GRANT SELECT ON forecast."Z_Top_N", forecast."Z_Display_by_city_country" TO grp_aviation_write;
  END IF;
END $$;
"""


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS powerbi")
    op.execute(_TOP_N)
    op.execute(_DISPLAY)
    op.execute(_DATES)
    op.execute(_GRANTS)
    # the two constants now live in `powerbi` — remove the forecast copies
    op.execute('DROP VIEW IF EXISTS forecast."Z_Top_N"')
    op.execute('DROP VIEW IF EXISTS forecast."Z_Display_by_city_country"')


def downgrade() -> None:
    op.execute(_FORECAST_TOP_N)
    op.execute(_FORECAST_DISPLAY)
    op.execute(_FORECAST_GRANTS)
    op.execute("DROP SCHEMA IF EXISTS powerbi CASCADE")
