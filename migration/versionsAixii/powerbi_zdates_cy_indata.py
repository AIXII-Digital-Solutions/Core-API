"""powerbi.z_dates_acys."Contract Year" — label a day ONLY with a Contract Year that actually exists in the
fact table; days outside get NULL.

The bug this fixes: z_dates_acys is bounded to WHOLE MONTHS (it has to be — acys_summary_grouped stamps its
"Date" on the 1st of the month, and clipping to the flights' real span would push those stamps out of the
calendar). But a Contract Year runs anchor-day to anchor-day, NOT month to month, so the whole-month bounds
poke out of both end CYs and mint two flightless stumps: CY2021 (14 days) at the bottom and CY2028 (16 days)
at the top. A slicer built on the column would then offer two years that show nothing — which is precisely
what the dropped forecast.z_contract_years existed to prevent.

Fix: LEFT JOIN the computed label against the distinct Contract Years present in acys_summary_by_day. Real
years label their days; the stumps come back NULL. The calendar stays contiguous (PBI requires that), and
`SELECT DISTINCT "Contract Year"` again yields exactly the years that have data.

Revision ID: powerbi_zdates_cy_indata
Revises: forecast_report_dims
Create Date: 2026-07-10
"""
from alembic import op

revision = "powerbi_zdates_cy_indata"
down_revision = "forecast_report_dims"
branch_labels = None
depends_on = None

# CY of a calendar day vs the request anchor: year(d) - 1 when (month, day) <= (anchor month, anchor day).
_CY = """'CY' || (extract(year from d."Date")::int - CASE
             WHEN (extract(month from d."Date")::int, extract(day from d."Date")::int)
                  <= (extract(month from a.d)::int, extract(day from a.d)::int)
             THEN 1 ELSE 0 END)::text"""

_BOUNDS = """
b AS (
    SELECT date_trunc('month', min("Date"))::date                      AS lo,
           (date_trunc('month', max("Date")) + INTERVAL '1 month'
                                             - INTERVAL '1 day')::date AS hi
    FROM forecast.acys_summary_grouped
),
anchor AS (   -- the request date, recovered from the data: actuals stop yesterday, the forecast starts today
    SELECT coalesce(
        (SELECT min("Date") FROM forecast.acys_summary_by_day WHERE "Data Type" = 'Forecast'),
        (SELECT max("Date") + 1 FROM forecast.acys_summary_by_day),
        DATE '2022-07-01'
    ) AS d
)"""

_NEW = f"""
CREATE OR REPLACE VIEW powerbi.z_dates_acys AS
WITH {_BOUNDS},
cys AS (   -- the Contract Years that actually have flights
    SELECT DISTINCT "Contract Year" cy
    FROM forecast.acys_summary_by_day
    WHERE "Contract Year" IS NOT NULL
)
SELECT d.*, cys.cy AS "Contract Year"
FROM powerbi.z_dates d
CROSS JOIN b
CROSS JOIN anchor a
LEFT JOIN cys ON cys.cy = {_CY}       -- a day in a flightless edge stump gets NULL, not a phantom year
WHERE d."Date" >= coalesce(b.lo, DATE '2022-07-01')
  AND d."Date" <= coalesce(b.hi, DATE '2029-12-31')
"""

_OLD = f"""
CREATE OR REPLACE VIEW powerbi.z_dates_acys AS
WITH {_BOUNDS}
SELECT d.*, {_CY} AS "Contract Year"
FROM powerbi.z_dates d
CROSS JOIN b
CROSS JOIN anchor a
WHERE d."Date" >= coalesce(b.lo, DATE '2022-07-01')
  AND d."Date" <= coalesce(b.hi, DATE '2029-12-31')
"""


def upgrade() -> None:
    op.execute(_NEW)


def downgrade() -> None:
    op.execute(_OLD)
