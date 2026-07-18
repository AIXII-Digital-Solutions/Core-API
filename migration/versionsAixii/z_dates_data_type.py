"""powerbi.z_dates_acys gains a "Data Type" column — 'Actuals' or 'Forecast' per calendar date.

The facts (acys_summary_by_day) carry a "Data Type" of 'Actuals'/'Forecast'; the dates dimension did not, so a
report could not colour or slice the calendar by which side of "today" a date sits on. The split is a single
cut DATE: the forecast starts the day AFTER the last actual (fc_start = last_fact + 1), so actuals and forecast
never share a day, and max(Actuals "Date") labels the whole daily calendar. A date is 'Actuals' iff it is
on/before that cut, else 'Forecast'. The gap day (last_fact + 1, when the monthly-dated forecast's first row
lands a day or two later) falls to Forecast — correct, since the forecast horizon opens there.

Builds on z_dates_cy_aligned_window: the window is unchanged, only the column is added. Source of truth is
`forecast_grouped_route_cols._Z_DATES_ACYS` (its _rebuild recreates this view) — the same change was made there.
This migration REPLAYS the definition onto the LIVE view (DROP + CREATE); no full chain rebuild is needed.
downgrade restores the CY-aligned window WITHOUT the column (the z_dates_cy_aligned_window definition).

Revision ID: z_dates_data_type
Revises: z_dates_cy_aligned_window
Create Date: 2026-07-17
"""
from alembic import op

revision = "z_dates_data_type"
down_revision = "z_dates_cy_aligned_window"
branch_labels = None
depends_on = None

# --- keep identical to forecast_grouped_route_cols._CY / _MONTH_SORT_IN_CY ---
_CY = """'CY' || (extract(year from d."Date")::int - CASE
             WHEN (extract(month from d."Date")::int, extract(day from d."Date")::int)
                  <= (extract(month from a.d)::int, extract(day from a.d)::int)
             THEN 1 ELSE 0 END)::text"""

_MONTH_SORT_IN_CY = """((extract(month from d."Date")::int
                         - extract(month from a.d)::int + 12) % 12) + 1"""

# The CY-aligned window CTEs, shared by both definitions below.
_WINDOW_CTES = """
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
)"""

# NEW — window + Data Type column.
_VIEW = f"""
CREATE VIEW powerbi.z_dates_acys AS
{_WINDOW_CTES},
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

# OLD — window WITHOUT the column (the z_dates_cy_aligned_window definition), for downgrade.
_VIEW_OLD = f"""
CREATE VIEW powerbi.z_dates_acys AS
{_WINDOW_CTES}
SELECT d.*, cys.cy AS "Contract Year",
       {_MONTH_SORT_IN_CY} AS "MonthSortInContractYear"
FROM powerbi.z_dates d
CROSS JOIN b
CROSS JOIN anchor a
LEFT JOIN cys ON cys.cy = {_CY}
WHERE d."Date" >= b.lo
  AND d."Date" <= b.hi
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
