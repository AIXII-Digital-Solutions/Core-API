"""powerbi.last_seen_fleet shows only aircraft the live poll has actually seen

The view listed every fleet aircraft and left the position columns empty for a tail FR24 had never
reported. Those rows are nearly all aircraft in storage that last flew before the live poll started
(2026-01-12) — a report of where the fleet is has nothing to say about them, and 23 empty rows made the
page harder to read than it needed to be.

So the position join becomes an INNER join: a fleet aircraft appears once it has at least one live
position. No date is written in: flightradar.livepositions begins with the live poll itself, so "has a
live position" and "has flown since the poll started" are the same test, and the rule keeps meaning
that however far the history grows. A tail that starts flying again comes back on its own.

The view text is the previous revision's, loaded by file path (alembic imports a revision as a
standalone module), with the one join changed and asserted.

Revision ID: pbi_fleet_seen_only
Revises: reg_airline_fallback
Create Date: 2026-09-18
"""
import importlib.util
import pathlib

from alembic import op

_spec = importlib.util.spec_from_file_location(
    "_reg_airline_fallback", pathlib.Path(__file__).with_name("reg_airline_fallback.py"))
_prev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_prev)
_PREV_VIEW = _prev.VIEW

revision = "pbi_fleet_seen_only"
down_revision = "reg_airline_fallback"
branch_labels = None
depends_on = None

_OUTER = "LEFT JOIN pos p ON p.reg_key = f.reg_key"
_INNER = ("-- only aircraft the live poll has seen at least once (livepositions starts with the poll)\n"
          "JOIN pos p ON p.reg_key = f.reg_key")
VIEW = _PREV_VIEW.replace(_OUTER, _INNER, 1)


def upgrade() -> None:
    assert _OUTER in _PREV_VIEW and _INNER in VIEW, "the previous view changed shape"
    op.execute("DROP VIEW IF EXISTS powerbi.last_seen_fleet")
    op.execute(VIEW)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS powerbi.last_seen_fleet")
    op.execute(_PREV_VIEW)
