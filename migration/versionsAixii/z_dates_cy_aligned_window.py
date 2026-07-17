"""powerbi.z_dates_acys — clip the dates window to whole CONTRACT YEARS (kills the spurious CY2021).

The Contract Year of a date is cut on the anchor day (as_of.day): CYn runs (anchor day + 1) of year n to
(anchor day) of year n+1. The view's window used to start at the month-start of the earliest fact (2022-07-01)
— which sits in the MIDDLE of a contract year, so its sub-boundary head (2022-07-01 .. anchor-day) labelled as
the PRIOR year, conjuring a CY2021 that has no full year of data. Symmetrically the old upper bound was the
month-END of the last date, overshooting the final CY boundary into a NULL-CY tail.

New window, both ends on a CY boundary:
  * lo = the first CY boundary AT/AFTER the fixed history floor 2022-07-01. With a 17-Sep anchor that is
    18-Sep of the first data year — the partial leading year is dropped, which is what "no CY2021" means.
  * hi = the anchor date itself (the horizon end IS a CY boundary = as_of.day) — no trailing partial year.
Verified against live data: zero DATED facts sit below the new lower bound, so only empty calendar dates are
trimmed, never facts. The old bound also read acys_summary_grouped, which carries stray 2003-era stub dates;
the new bound is computed from the anchor + the constant floor and does not touch that table.

NOTE ON OWNERSHIP: `forecast_grouped_route_cols._Z_DATES_ACYS` is the SOURCE OF TRUTH for this view — its
_rebuild() recreates z_dates_acys, so the same change was made there too. This migration only REPLAYS the new
definition onto the LIVE view (DROP + CREATE), so no full chain rebuild (which would drop and re-CONCURRENTLY
-build the flightsummary index and the matview) is needed. Keep the two in sync.

Revision ID: z_dates_cy_aligned_window
Revises: z_dates_month_sort_in_cy
Create Date: 2026-07-17
"""
from alembic import op

revision = "z_dates_cy_aligned_window"
down_revision = "z_dates_month_sort_in_cy"
branch_labels = None
depends_on = None

# --- keep identical to forecast_grouped_route_cols._CY / _MONTH_SORT_IN_CY ---
_CY = """'CY' || (extract(year from d."Date")::int - CASE
             WHEN (extract(month from d."Date")::int, extract(day from d."Date")::int)
                  <= (extract(month from a.d)::int, extract(day from a.d)::int)
             THEN 1 ELSE 0 END)::text"""

_MONTH_SORT_IN_CY = """((extract(month from d."Date")::int
                         - extract(month from a.d)::int + 12) % 12) + 1"""

# NEW — contract-year-aligned window (see module docstring).
_VIEW = f"""
CREATE VIEW powerbi.z_dates_acys AS
WITH anchor AS (
    SELECT coalesce(
        (SELECT max("Date") FROM forecast.acys_summary_by_day),
        DATE '2022-07-01'
    ) AS d
),
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
)
SELECT d.*, cys.cy AS "Contract Year",
       {_MONTH_SORT_IN_CY} AS "MonthSortInContractYear"
FROM powerbi.z_dates d
CROSS JOIN b
CROSS JOIN anchor a
LEFT JOIN cys ON cys.cy = {_CY}
WHERE d."Date" >= b.lo
  AND d."Date" <= b.hi
"""

# OLD — the month-range window (for downgrade). Identical to the pre-change definition.
_VIEW_OLD = f"""
CREATE VIEW powerbi.z_dates_acys AS
WITH b AS (
    SELECT date_trunc('month', min("Date"))::date                      AS lo,
           (date_trunc('month', max("Date")) + INTERVAL '1 month'
                                             - INTERVAL '1 day')::date AS hi
    FROM forecast.acys_summary_grouped
),
anchor AS (
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
    op.execute(_VIEW_OLD)
    op.execute(_GRANTS)
