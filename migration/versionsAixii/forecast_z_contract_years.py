"""forecast.z_contract_years — a one-column disconnected lookup for PBI: the distinct "Contract Year"
values actually present in the dataset (CY2022 … CY<as_of.year + horizon − 1>), so a slicer never offers a
year with no rows.

Sourced from forecast.acys_summary_grouped (the matview), NOT from acys_summary_by_day, on purpose: the
slicer must offer exactly the years the fact table can serve. The matview is refreshed by the worker right
after acys_summary_by_day is filled, so the two are always in step.

Revision ID: forecast_z_contract_years
Revises: forecast_grouped_matview
Create Date: 2026-07-10
"""
from alembic import op

revision = "forecast_z_contract_years"
down_revision = "forecast_grouped_matview"
branch_labels = None
depends_on = None

_VIEW = """
CREATE VIEW forecast.z_contract_years AS
SELECT DISTINCT "Contract Year"
FROM forecast.acys_summary_grouped
WHERE "Contract Year" IS NOT NULL
ORDER BY 1
"""

_GRANTS = """
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aixii_read') THEN
    GRANT SELECT ON forecast.z_contract_years TO grp_aixii_read;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aviation_write') THEN
    GRANT SELECT ON forecast.z_contract_years TO grp_aviation_write;
  END IF;
END $$;
"""


def upgrade() -> None:
    op.execute(_VIEW)
    op.execute(_GRANTS)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS forecast.z_contract_years")
