"""forecast.aircraft_information gains "Owner", "Manager", "MSN" — current per-tail Cirium attributes.

Like the existing "Current Family", these come from Cirium's newest snapshot per registration (the matview's
cur_family CTE, latest revision per plan_type then newest per tail): "Owner" / "Manager" verbatim, "MSN" =
Cirium "Serial Number". Tails absent from the current Cirium roster (wet-lease / carry-forward) get NULL, same
as the other current attributes.

aircraft_information is a LEAF matview (nothing reads it), so it is simply dropped and recreated. The new
definition is IMPORTED from the source of truth forecast_grouped_route_cols._AIRCRAFT_INFO (which was updated
in lockstep), so this migration never duplicates the (helper-built) SQL and cannot drift from it.

Revision ID: aircraft_info_cirium_fields
Revises: age_group_sort_facts
Create Date: 2026-07-20
"""
import os
import sys

import sqlalchemy as sa
from alembic import op

# the source-of-truth chain module lives beside this migration; put versionsAixii on sys.path so we can
# import the CANONICAL aircraft_information definition + its index list instead of copying the SQL.
sys.path.insert(0, os.path.dirname(__file__))
from forecast_grouped_route_cols import _AIRCRAFT_INFO, _AIRCRAFT_INFO_INDEXES  # noqa: E402

revision = "aircraft_info_cirium_fields"
down_revision = "age_group_sort_facts"
branch_labels = None
depends_on = None

_OBJ = "forecast.aircraft_information"
_NEW_COLS = ("Owner", "Manager", "MSN")

_OWNER = """
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aviation_write') THEN
    EXECUTE 'ALTER MATERIALIZED VIEW forecast.aircraft_information OWNER TO grp_aviation_write';
  END IF;
END $$;
"""

_GRANTS = """
DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['grp_aixii_read','grp_aviation_write'] LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('GRANT SELECT ON forecast.aircraft_information TO %I', r);
    END IF;
  END LOOP;
END $$;
"""


def _finish() -> None:
    for ix in _AIRCRAFT_INFO_INDEXES:   # Owner/Manager/MSN are not indexed; same list both directions
        op.execute(ix)
    op.execute(_OWNER)
    op.execute(_GRANTS)


def upgrade() -> None:
    op.execute(f"DROP MATERIALIZED VIEW IF EXISTS {_OBJ}")
    op.execute(_AIRCRAFT_INFO)          # the updated source-of-truth def (with Owner/Manager/MSN)
    _finish()


def downgrade() -> None:
    # Rebuild WITHOUT the three new columns by wrapping the current def and selecting every column except them
    # (the source of truth now carries them, so we can't just replay an "old" string).
    conn = op.get_bind()
    defn = conn.execute(sa.text("SELECT pg_get_viewdef(:o ::regclass, true)"), {"o": _OBJ}).scalar()
    defn = defn.strip().rstrip(";").strip()
    cols = [r[0] for r in conn.execute(sa.text(
        "SELECT a.attname FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'forecast' AND c.relname = 'aircraft_information' "
        "AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum"))]
    keep = ", ".join(f'sub."{c}"' for c in cols if c not in _NEW_COLS)
    op.execute(f"DROP MATERIALIZED VIEW IF EXISTS {_OBJ}")
    op.execute(f"CREATE MATERIALIZED VIEW {_OBJ} AS SELECT {keep} FROM ({defn}) sub")
    _finish()
