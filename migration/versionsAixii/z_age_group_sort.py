"""powerbi.z_age_group gains a numeric "Age Group Sort" (1..10) so PowerBI can sort the bands correctly.

"Age Group" values are "1. …" .. "10. …". PowerBI sorts them as TEXT, so "10. …" falls between "1. …" and
"2. …" — the bands come out 1,10,2,3,…,9. The fix is the standard PowerBI "sort by column": add a numeric
key parsed from the "N. " prefix and, in PowerBI, set Sort "Age Group" BY "Age Group Sort". (Same pattern as
powerbi.z_dates_acys."MonthSortInContractYear".) The view is also ORDER BY-ed so a raw SQL read returns 1..10,
though PowerBI ignores source order — the sort-by-column setting is what actually reorders the visual.

NOTE ON OWNERSHIP: forecast_grouped_route_cols._Z_AGE_GROUP is the SOURCE OF TRUTH — its _rebuild recreates
z_age_group, so the same change was made there. This migration only REPLAYS the new definition onto the LIVE
view (z_age_group is a leaf VIEW — nothing depends on it — so a plain DROP+CREATE needs no chain rebuild).

Revision ID: z_age_group_sort
Revises: cirium_matviews_owner
Create Date: 2026-07-20
"""
from alembic import op

revision = "z_age_group_sort"
down_revision = "cirium_matviews_owner"
branch_labels = None
depends_on = None

_NEW = """
CREATE VIEW powerbi.z_age_group AS
SELECT v."Age Group",
       split_part(v."Age Group", '.', 1)::int AS "Age Group Sort"
FROM (VALUES
    ('1. Less than one year'),
    ('2. From 1 to 2 years'),
    ('3. From 2 to 4 years'),
    ('4. From 4 to 6 years'),
    ('5. From 6 to 8 years'),
    ('6. From 8 to 10 years'),
    ('7. From 10 to 12 years'),
    ('8. From 12 to 14 years'),
    ('9. From 14 to 16 years'),
    ('10. More than 16 years')
) AS v("Age Group")
ORDER BY 2
"""

_OLD = """
CREATE VIEW powerbi.z_age_group AS
SELECT v."Age Group" FROM (VALUES
    ('1. Less than one year'),
    ('2. From 1 to 2 years'),
    ('3. From 2 to 4 years'),
    ('4. From 4 to 6 years'),
    ('5. From 6 to 8 years'),
    ('6. From 8 to 10 years'),
    ('7. From 10 to 12 years'),
    ('8. From 12 to 14 years'),
    ('9. From 14 to 16 years'),
    ('10. More than 16 years')
) AS v("Age Group")
"""

_GRANTS = """
DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['grp_aixii_read','grp_aviation_write'] LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('GRANT SELECT ON powerbi.z_age_group TO %I', r);
    END IF;
  END LOOP;
END $$;
"""


def upgrade() -> None:
    op.execute("DROP VIEW IF EXISTS powerbi.z_age_group")
    op.execute(_NEW)
    op.execute(_GRANTS)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS powerbi.z_age_group")
    op.execute(_OLD)
    op.execute(_GRANTS)
