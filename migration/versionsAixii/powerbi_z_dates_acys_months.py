"""Widen powerbi.z_dates_acys to WHOLE MONTHS: lower bound = first day of the earliest month in
forecast.acys_summary_grouped, upper bound = LAST day of its latest month.

Why not the raw min/max: acys_summary_grouped."Date" is `to_date("Period",'MM-YYYY')` — always the 1st of
the month — so a literal max() lands on the 1st of the final month and cuts that month short (measured: 9
real flight days, 02..10-Sep-2028, fell outside the calendar).

And bounding by acys_summary_by_day's real flight days is the opposite trap: its earliest day (11-Sep-2022)
is LATER than grouped's month stamp (01-Sep-2022), which would push grouped's whole first month out of the
calendar and into PBI's blank date row.

Whole months satisfy BOTH fact tables at once — grouped's month-start stamps and by_day's real daily dates
always fall inside [first-of-first-month, last-of-last-month] — while still reading only grouped, whose
"Date" is indexed (ix_acys_grouped_date), so the bound costs two index probes instead of a 400k-row scan.

NOTE: revision ids are capped at 32 chars (alembic_version.version_num is varchar(32)).

Revision ID: powerbi_z_dates_acys_months
Revises: powerbi_z_dates_acys
Create Date: 2026-07-10
"""
from alembic import op

revision = "powerbi_z_dates_acys_months"
down_revision = "powerbi_z_dates_acys"
branch_labels = None
depends_on = None

# CREATE OR REPLACE keeps the column list identical (SELECT d.*), so PBI sees no schema change.
_NEW = """
CREATE OR REPLACE VIEW powerbi.z_dates_acys AS
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

_OLD = """
CREATE OR REPLACE VIEW powerbi.z_dates_acys AS
SELECT d.*
FROM powerbi.z_dates d
CROSS JOIN (
    SELECT min("Date") AS lo, max("Date") AS hi
    FROM forecast.acys_summary_grouped
) b
WHERE d."Date" >= coalesce(b.lo, DATE '2022-07-01')
  AND d."Date" <= coalesce(b.hi, DATE '2029-12-31')
"""


def upgrade() -> None:
    op.execute(_NEW)


def downgrade() -> None:
    op.execute(_OLD)
