"""Two single-column lookup views for PBI slicers, over forecast.acys_summary_by_day (read from the base
table, not the acys_summary_grouped VIEW: we only need DISTINCT City/Country, so scanning the table is
cheaper than re-aggregating the grouped view):

  * forecast.acys_origin_bucket      — one column "Origin Bucket",
  * forecast.acys_destination_bucket — one column "Destination Bucket",
each listing every distinct 'City (Country)' AND every distinct standalone 'Country' of that side, plus a
literal 'Others' row appended at the END (the view ORDER BY sorts real values alphabetically, then 'Others'
last). Empty/NULL city or country rows are excluded.

Revision ID: forecast_location_value_views
Revises: forecast_move_af_matviews
Create Date: 2026-07-09
"""
from alembic import op

revision = "forecast_location_value_views"
down_revision = "forecast_move_af_matviews"
branch_labels = None
depends_on = None


def _view(name: str, col: str, city: str, country: str) -> str:
    return f"""
CREATE VIEW forecast.{name} AS
SELECT "{col}" FROM (
    SELECT DISTINCT nullif("{city}",'') || ' (' || nullif("{country}",'') || ')' AS "{col}"
    FROM forecast.acys_summary_by_day
    WHERE nullif("{city}",'') IS NOT NULL AND nullif("{country}",'') IS NOT NULL
    UNION
    SELECT DISTINCT nullif("{country}",'')
    FROM forecast.acys_summary_by_day
    WHERE nullif("{country}",'') IS NOT NULL
    UNION
    SELECT 'Others'
) t
ORDER BY ("{col}" = 'Others'), "{col}"
"""


_GRANTS = """
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aixii_read') THEN
    GRANT SELECT ON forecast.acys_origin_bucket, forecast.acys_destination_bucket TO grp_aixii_read;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aviation_write') THEN
    GRANT SELECT ON forecast.acys_origin_bucket, forecast.acys_destination_bucket TO grp_aviation_write;
  END IF;
END $$;
"""


def upgrade() -> None:
    op.execute(_view("acys_origin_bucket", "Origin Bucket", "Origin City", "Origin Country"))
    op.execute(_view("acys_destination_bucket", "Destination Bucket", "Destination City", "Destination Country"))
    op.execute(_GRANTS)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS forecast.acys_origin_bucket")
    op.execute("DROP VIEW IF EXISTS forecast.acys_destination_bucket")
