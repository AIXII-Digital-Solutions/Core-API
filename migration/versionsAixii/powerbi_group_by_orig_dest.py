"""powerbi.group_by_orig_dest — a static slicer dimension: one column "Group By", rows 'Origin' / 'Destination'.

Same shape as the existing powerbi.z_display_by_city_country / z_age_group slicers: a VALUES-backed view with
no dependency on any fact table, so it is never invalidated by a matview rebuild and needs no refresh. It lets
a report toggle whether the geography dimension is read from the ORIGIN side or the DESTINATION side of a
flight (acys_summary_grouped carries both: "Origin City&Country" / "Destination City&Country", "Origin
Country" / "Destination Country", ...).

Grants mirror the rest of the powerbi schema: SELECT for grp_aixii_read and grp_aviation_write.

Revision ID: powerbi_group_by_orig_dest
Revises: fr24_drop_unused_indexes
Create Date: 2026-07-17
"""
from alembic import op

revision = "powerbi_group_by_orig_dest"
down_revision = "fr24_drop_unused_indexes"
branch_labels = None
depends_on = None

_VIEW = """
CREATE VIEW powerbi.group_by_orig_dest AS
SELECT v."Group By"
FROM (VALUES ('Origin'), ('Destination')) AS v("Group By")
"""

_GRANTS = """
DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['grp_aixii_read','grp_aviation_write'] LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('GRANT SELECT ON powerbi.group_by_orig_dest TO %I', r);
    END IF;
  END LOOP;
END $$;
"""


def upgrade() -> None:
    op.execute("DROP VIEW IF EXISTS powerbi.group_by_orig_dest")
    op.execute(_VIEW)
    op.execute(_GRANTS)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS powerbi.group_by_orig_dest")
