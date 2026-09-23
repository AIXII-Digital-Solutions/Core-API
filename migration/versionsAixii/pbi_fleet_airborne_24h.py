"""powerbi.last_seen_fleet: "Airborne in the last 24h" replaces "Stationary for more than 24h"

The 24h flag the report asked for is not "silent for a day" but "flew in the last day", by the rule
of the report's own DAX measure (Aircrafts Airborne in the last 24h):

  * the window is anchored at the NEWEST livepositions row, not now(), so a stalled poll does not
    turn the whole fleet "not airborne";
  * a position counts as airborne at ground speed >= 100 kt OR altitude > 9 ft — the DAX thresholds
    as written;
  * the tail is matched by the exact registration the live poll stores, as the Flown Path is.

The set of airborne registrations is computed once for the whole view (the created_at BRIN index
narrows it to the window), not once per tail.

The view text is the previous revision's, loaded by file path, with three anchored edits — the CTE,
the column it carries through `base`, and the output column — each asserted.

Revision ID: pbi_fleet_airborne_24h
Revises: pbi_fleet_stationary_24h
Create Date: 2026-09-23
"""
import importlib.util
import pathlib

from alembic import op

_spec = importlib.util.spec_from_file_location(
    "_pbi_fleet_stationary_24h", pathlib.Path(__file__).with_name("pbi_fleet_stationary_24h.py"))
_prev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_prev)
_PREV_VIEW = _prev.VIEW

revision = "pbi_fleet_airborne_24h"
down_revision = "pbi_fleet_stationary_24h"
branch_labels = None
depends_on = None

_READ_ROLES = _prev._READ_ROLES
_VIEW_COMMENT = _prev._VIEW_COMMENT

_WINDOW_HOURS = 24
_AIRBORNE_KNOTS = 100
_AIRBORNE_ALT_FT = 9

# 1. the registrations seen airborne inside the window ending at the newest position
_BASE_CTE = "base AS (\n"
_BASE_CTE_NEW = f"""airborne_24h AS (
    SELECT DISTINCT l.reg
    FROM flightradar.livepositions l,
         (SELECT max(created_at) AS ts FROM flightradar.livepositions) anchor
    WHERE l.created_at >= anchor.ts - interval '{_WINDOW_HOURS} hours'
      AND l.created_at <= anchor.ts
      AND (l.gspeed >= {_AIRBORNE_KNOTS} OR l.alt > {_AIRBORNE_ALT_FT})
),
""" + _BASE_CTE

# 2. carry the flag through base, where the live registration is still in scope
_BASE_COLS = "           p.last_seen, p.lat, p.lon, p.alt, p.gspeed,\n"
_BASE_COLS_NEW = _BASE_COLS + (
    "           EXISTS (SELECT 1 FROM airborne_24h a WHERE a.reg = p.seen_reg) AS airborne_24h,\n")

# 3. the output column, in place of the stationary one
_OUT_OLD = """    (last_seen < now() - interval '24 hours') AS "Stationary for more than 24h"\n"""
_OUT_NEW = f"""    airborne_24h                                      AS "Airborne in the last {_WINDOW_HOURS}h"\n"""

VIEW = (_PREV_VIEW
        .replace(_BASE_CTE, _BASE_CTE_NEW, 1)
        .replace(_BASE_COLS, _BASE_COLS_NEW, 1)
        .replace(_OUT_OLD, _OUT_NEW, 1))


def upgrade() -> None:
    for anchor in (_BASE_CTE, _BASE_COLS, _OUT_OLD):
        assert _PREV_VIEW.count(anchor) == 1, "the previous view changed shape around " + anchor.strip()
    assert VIEW.count('"Airborne in the last 24h"') == 1 and "Stationary for more than 24h" not in VIEW

    op.execute("DROP VIEW IF EXISTS powerbi.last_seen_fleet")
    op.execute(VIEW)
    # a DROP takes the comment and the ACL with it
    op.execute(f"COMMENT ON VIEW powerbi.last_seen_fleet IS {_VIEW_COMMENT}")
    op.execute(f"GRANT SELECT ON powerbi.last_seen_fleet TO {_READ_ROLES}")


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS powerbi.last_seen_fleet")
    op.execute(_PREV_VIEW)
    op.execute(f"COMMENT ON VIEW powerbi.last_seen_fleet IS {_VIEW_COMMENT}")
    op.execute(f"GRANT SELECT ON powerbi.last_seen_fleet TO {_READ_ROLES}")
