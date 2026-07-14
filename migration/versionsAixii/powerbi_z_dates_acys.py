"""powerbi.z_dates_acys — powerbi.z_dates clipped to the span the ACYS dataset actually covers
(min/max "Date" in forecast.acys_summary_grouped), for reports that must not offer empty calendar tails.

Built ON TOP of powerbi.z_dates (`SELECT d.*`) rather than re-deriving the 17 columns: the calendar is
defined in exactly one place, so the two can never drift apart.

NOTE on the upper bound: acys_summary_grouped."Date" is `to_date("Period",'MM-YYYY')` — the FIRST of the
month, not a real flight day. So the range ends on the 1st of the last forecast month. That matches the
fact table it is meant to slice (grouped only ever holds month-start dates). If it is ever pointed at
acys_summary_by_day (real daily dates), the bound would need extending to that month's last day.

If acys_summary_grouped is empty (fresh DB, no run yet), min/max are NULL and the view would collapse to
zero rows — a broken date table in PBI. The COALESCE fallback keeps the full z_dates span in that case.

Also indexes acys_summary_grouped("Date") so the min/max bound is an index probe, not a 184k-row scan.

Revision ID: powerbi_z_dates_acys
Revises: powerbi_schema
Create Date: 2026-07-10
"""
from alembic import op

revision = "powerbi_z_dates_acys"
down_revision = "powerbi_schema"
branch_labels = None
depends_on = None

_VIEW = """
CREATE VIEW powerbi.z_dates_acys AS
SELECT d.*
FROM powerbi.z_dates d
CROSS JOIN (
    SELECT min("Date") AS lo, max("Date") AS hi
    FROM forecast.acys_summary_grouped
) b
WHERE d."Date" >= coalesce(b.lo, DATE '2022-07-01')
  AND d."Date" <= coalesce(b.hi, DATE '2029-12-31')
"""

_GRANTS = """
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aixii_read') THEN
    GRANT SELECT ON powerbi.z_dates_acys TO grp_aixii_read;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aviation_write') THEN
    GRANT SELECT ON powerbi.z_dates_acys TO grp_aviation_write;
  END IF;
END $$;
"""


def upgrade() -> None:
    op.execute('CREATE INDEX IF NOT EXISTS ix_acys_grouped_date ON forecast.acys_summary_grouped ("Date")')
    op.execute(_VIEW)
    op.execute(_GRANTS)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS powerbi.z_dates_acys")
    op.execute("DROP INDEX IF EXISTS forecast.ix_acys_grouped_date")
