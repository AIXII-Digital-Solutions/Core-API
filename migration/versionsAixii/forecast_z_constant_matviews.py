"""Two constant lookup views for PBI slicers (disconnected tables):
  * forecast."Z_Top_N"                    — "Value" (1..7) -> "Select TopN Countries/Cities to display"
                                            (3, 5, 10, 15, 20, 30, 40),
  * forecast."Z_Display_by_city_country"  — "Display Chart by:" with the two values 'By Country' / 'By City'.
Static constants — a plain view returns them with no storage or refresh.

Revision ID: forecast_z_constant_matviews
Revises: forecast_grouped_agreed_value
Create Date: 2026-07-09
"""
from alembic import op

revision = "forecast_z_constant_matviews"
down_revision = "forecast_grouped_agreed_value"
branch_labels = None
depends_on = None

_TOP_N = """
CREATE VIEW forecast."Z_Top_N" AS
SELECT * FROM (VALUES (1, 3), (2, 5), (3, 10), (4, 15), (5, 20), (6, 30), (7, 40))
    AS t("Value", "Select TopN Countries/Cities to display")
"""

_DISPLAY = """
CREATE VIEW forecast."Z_Display_by_city_country" AS
SELECT * FROM (VALUES ('By Country'), ('By City')) AS t("Display Chart by:")
"""

_GRANTS = """
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
    op.execute(_TOP_N)
    op.execute(_DISPLAY)
    op.execute(_GRANTS)


def downgrade() -> None:
    op.execute('DROP VIEW IF EXISTS forecast."Z_Top_N"')
    op.execute('DROP VIEW IF EXISTS forecast."Z_Display_by_city_country"')
