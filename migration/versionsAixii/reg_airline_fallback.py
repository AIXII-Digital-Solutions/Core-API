"""api.registration carries its own airline name, and the report falls back to it

A hand-listed tail reaches cirium.non_asg_insured_* without an airline: it is there BECAUSE no name in
api.airlines matched it, so the matview's `Airline` is null and the report showed an empty cell. The
operator is whoever Cirium says flies it, which is not the same question as "whose aircraft is this".

api.registration therefore gains `airline`, filled in beside the registration, and the view reads
Airline Name as "the matview's match, or failing that the name typed next to the registration".
Nothing else changes: a tail matched through api.airlines keeps that name, and the column stays NULL
for anyone who does not care to fill it.

Revision ID: reg_airline_fallback
Revises: pbi_fleet_view_v2
Create Date: 2026-09-17
"""
import importlib.util
import pathlib

from alembic import op

# Alembic imports a revision as a standalone module, so the sibling cannot be imported by package
# path. Load it by file path instead of copying 130 lines of SQL that would then drift.
_spec = importlib.util.spec_from_file_location(
    "_pbi_fleet_view_v2", pathlib.Path(__file__).with_name("pbi_fleet_view_v2.py"))
_v2 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_v2)
_V2_VIEW = _v2.VIEW

revision = "reg_airline_fallback"
down_revision = "pbi_fleet_view_v2"
branch_labels = None
depends_on = None

# The view is the v2 text with two edits: the fleet CTEs carry the registration through, and
# Airline Name coalesces the matview's match onto the hand-typed name.
_JOIN = """FROM fleet_one f
LEFT JOIN api.registration manual ON upper(regexp_replace(manual.reg, '[^A-Za-z0-9]', '', 'g')) = f.reg_key
LEFT JOIN pos p ON p.reg_key = f.reg_key"""

VIEW = (_V2_VIEW
        .replace('    f.airline_name                                    AS "Airline Name",',
                 '    coalesce(f.airline_name, manual.airline)           AS "Airline Name",', 1)
        .replace("FROM fleet_one f\nLEFT JOIN pos p ON p.reg_key = f.reg_key", _JOIN, 1))


def upgrade() -> None:
    assert "manual.airline" in VIEW and "api.registration manual" in VIEW, \
        "the v2 view text changed shape — rebuild this migration against it"
    op.execute("ALTER TABLE api.registration ADD COLUMN IF NOT EXISTS airline text")
    op.execute("COMMENT ON COLUMN api.registration.airline IS "
               "'Whose aircraft this is, for a tail no api.airlines name matches. "
               "powerbi.last_seen_fleet shows it as Airline Name when the matview has none.'")
    op.execute("DROP VIEW IF EXISTS powerbi.last_seen_fleet")
    op.execute(VIEW)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS powerbi.last_seen_fleet")
    op.execute(_V2_VIEW)
    op.execute("ALTER TABLE api.registration DROP COLUMN IF EXISTS airline")
