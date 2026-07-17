"""powerbi.z_dates_acys gains "MonthSortInContractYear" — the month's ordinal INSIDE the Contract Year.

A report ordering months Jan..Dec is wrong for a contract that runs Sep..Aug. This column sorts them the way
the contract runs: the CY opens in the anchor month (= the request's as_of month), so with a September anchor
Sep -> 1, Oct -> 2, Nov -> 3, ... Aug -> 12. It is pure month arithmetic modulo 12 against the same anchor
that already derives "Contract Year" in this view, so the two columns can never disagree about where a CY
starts.

The CY boundary is DAY-precise, so the anchor month appears at BOTH ends of a Contract Year (CY2025 =
18-Sep-2025 .. 17-Sep-2026) and both halves get ordinal 1 — which is exactly what "September is month 1 of the
contract year" means. Slicing by "Contract Year" too (as the report does) keeps the halves apart.

It lives in z_dates_acys, not z_dates: the ordinal needs the CY anchor, and z_dates is the plain calendar with
no contract concept.

NOTE ON OWNERSHIP: `forecast_grouped_route_cols._Z_DATES_ACYS` is the SOURCE OF TRUTH for this view — its
_drop_chain()/_rebuild() recreate z_dates_acys, so a definition missing there silently reverts on the next
chain rebuild. The column was added there too. This migration only replays the new definition onto the LIVE
view, so no full chain rebuild (which would drop and re-CONCURRENTLY-build the 255 MB flightsummary index and
the matview) was needed. Keep the two in sync.

Revision ID: z_dates_month_sort_in_cy
Revises: airport_geo_matviews
Create Date: 2026-07-17
"""
from alembic import op

revision = "z_dates_month_sort_in_cy"
down_revision = "airport_geo_matviews"
branch_labels = None
depends_on = None

# --- keep identical to forecast_grouped_route_cols._CY / _MONTH_SORT_IN_CY / _Z_DATES_ACYS ---
_CY = """'CY' || (extract(year from d."Date")::int - CASE
             WHEN (extract(month from d."Date")::int, extract(day from d."Date")::int)
                  <= (extract(month from a.d)::int, extract(day from a.d)::int)
             THEN 1 ELSE 0 END)::text"""

_MONTH_SORT_IN_CY = """((extract(month from d."Date")::int
                         - extract(month from a.d)::int + 12) % 12) + 1"""

_VIEW = f"""
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
SELECT d.*, cys.cy AS "Contract Year",
       {_MONTH_SORT_IN_CY} AS "MonthSortInContractYear"
FROM powerbi.z_dates d
CROSS JOIN b
CROSS JOIN anchor a
LEFT JOIN cys ON cys.cy = {_CY}
WHERE d."Date" >= coalesce(b.lo, DATE '2022-07-01')
  AND d."Date" <= coalesce(b.hi, DATE '2029-12-31')
"""

_VIEW_NO_SORT = _VIEW.replace(f',\n       {_MONTH_SORT_IN_CY} AS "MonthSortInContractYear"', "")

_GRANTS = """
DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['grp_aixii_read','grp_aviation_write'] LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('GRANT SELECT ON powerbi.z_dates_acys TO %I', r);
    END IF;
  END LOOP;
END $$;
"""


def upgrade() -> None:
    op.execute("DROP VIEW IF EXISTS powerbi.z_dates_acys")
    op.execute(_VIEW)
    op.execute(_GRANTS)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS powerbi.z_dates_acys")
    op.execute(_VIEW_NO_SORT)
    op.execute(_GRANTS)
