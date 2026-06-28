"""rename aixii tables to match class names (singular, no inflect pluralization)

Revision ID: 9a1b2c3d4e5f
Revises: 3f7f41318c02
Create Date: 2026-06-27

Hand-written. BaseMixin.__tablename__ changed from inflect-pluralized to the bare lowercased
class name (`Airlines` -> `airlines`, `CiriumAircrafts` -> `ciriumaircrafts`, ...). This renames
the already-created tables to match. Only the "keeper" tables are renamed here:
`asgaircraft` / `ciriumaircraftsdelta` are dropped and replaced by materialized views in the next
revision, so they are intentionally NOT renamed.

NOTE: only table names change. Their indexes / sequences / PK constraints keep their old
(old-table-derived) names — they stay fully functional (Postgres attaches them by OID, not name).
Index names are therefore stylistically tied to the old table names; normalizing them is a
separate, optional follow-up.

`service` DB is untouched (its models use explicit __tablename__, so the inflect change does not
affect them).
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "9a1b2c3d4e5f"
down_revision = "3f7f41318c02"
branch_labels = None
depends_on = None


# (schema, old_name, new_name) — keeper tables only.
RENAMES = [
    ("cirium",       "aircraftrevisions",   "aircraftrevision"),
    ("cirium",       "ciriumaircraft",      "ciriumaircrafts"),
    ("airlabs",      "flightsnapshots",     "flightsnapshot"),
    ("airlabs",      "aircraftstates",      "aircraftstate"),
    ("flightradar",  "flightsummaries",     "flightsummary"),
    ("flightradar",  "liveposition",        "livepositions"),
    ("flightradar",  "airports",            "airport"),
    ("flightradar",  "airportrunways",      "airportrunway"),
    ("aviationedge", "historicalschedules", "historicalschedule"),
    ("api",          "airline",             "airlines"),
]


def upgrade() -> None:
    for schema, old, new in RENAMES:
        op.rename_table(old, new, schema=schema)


def downgrade() -> None:
    for schema, old, new in reversed(RENAMES):
        op.rename_table(new, old, schema=schema)
