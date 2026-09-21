"""api.airlines becomes ref.airline

The insured-aircraft domain is built around the airline: `fleet.aircraft` points at it, the report
reads it, and the cirium matviews resolve their operator strings against it. Leaving it alone in
`api` — a schema that now holds nothing else but the FlightRadar tracking list — made the domain
look as if it borrowed its own centre from somewhere else.

THE MOVE IS A CATALOGUE UPDATE. `ALTER TABLE … SET SCHEMA` copies no data, keeps ids and the
sequence position, and carries the table's own grants along. The four matviews that read it —
cirium.asg_commercial / asg_business_helicopters / non_asg_insured_commercial /
non_asg_insured_business — keep working WITHOUT being rebuilt, because PostgreSQL records a view's
dependencies by OID, not by name; their stored definitions simply start printing `ref.airline`. The
one foreign key pointing at it (fleet.aircraft.airline_id) follows for the same reason. No raw SQL
in core-api or external-worker names the table, so nothing else has to change.

RENAMED TO THE SINGULAR `airline`, matching every other table in the rebuilt domain (party,
aircraft, policy, coverage, agreement). Its primary key and sequence were ALREADY called
`airline_pkey` / `airline_id_seq` from an earlier life, so this actually ends the inconsistency
rather than starting one.

The three implicit indexes are renamed with it. SQLAlchemy derives an implicit index name from the
table's SCHEMA and NAME (`ix_<schema>_<table>_<column>`), so without the rename every future
autogenerate would propose dropping `ix_api_airlines_*` and creating `ix_ref_airline_*`, forever —
the same trap `insurance_schema_move` documented.

Revision ID: airlines_to_ref
Revises: insured_fleet_rebuild
Create Date: 2026-09-21
"""
from alembic import op

revision = "airlines_to_ref"
down_revision = "insured_fleet_rebuild"
branch_labels = None
depends_on = None

# old implicit name -> new implicit name (ix_<schema>_<table>_<column>)
_INDEXES = {
    "ix_api_airlines_airline_name": "ix_ref_airline_airline_name",
    "ix_api_airlines_icao": "ix_ref_airline_icao",
    "ix_api_airlines_iata": "ix_ref_airline_iata",
}

# The audit trigger travels with the table (a trigger belongs to its table), but keeps its old name.
_TRIGGER_OLD, _TRIGGER_NEW = "airlines_audit", "airline_audit"

# Read roles, as the rest of the domain grants them. The table's own ACL comes along with the move,
# so these are belt and braces — and they are what makes a rebuilt cluster match a migrated one.
_READ_ROLES = "grp_aixii_read, grp_aviation_write, svc_external_worker"
_WRITE_ROLE = "grp_api_write"


def upgrade() -> None:
    op.execute("ALTER TABLE api.airlines SET SCHEMA ref")
    op.execute("ALTER TABLE ref.airlines RENAME TO airline")
    for old, new in _INDEXES.items():
        op.execute(f"ALTER INDEX IF EXISTS ref.{old} RENAME TO {new}")
    op.execute(f"ALTER TRIGGER {_TRIGGER_OLD} ON ref.airline RENAME TO {_TRIGGER_NEW}")

    op.execute(f"GRANT SELECT ON ref.airline TO {_READ_ROLES}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ref.airline TO {_WRITE_ROLE}")
    op.execute(f"GRANT USAGE, SELECT ON SEQUENCE ref.airline_id_seq TO {_WRITE_ROLE}")

    op.execute(
        "COMMENT ON TABLE ref.airline IS "
        "'The airlines this business insures or tracks - a hand-kept reference, not a directory. "
        "Moved from api.airlines by revision airlines_to_ref. Operator strings from Cirium are "
        "matched against it by SUBSTRING, longest name first, so the names here stay short.'"
    )
    op.execute(
        "COMMENT ON SCHEMA api IS "
        "'What is left of the old API-owned reference schema: api.registration, the hand-kept list "
        "of tails to poll FlightRadar for. The airline reference moved to ref.airline.'"
    )


def downgrade() -> None:
    op.execute(f"ALTER TRIGGER {_TRIGGER_NEW} ON ref.airline RENAME TO {_TRIGGER_OLD}")
    for old, new in _INDEXES.items():
        op.execute(f"ALTER INDEX IF EXISTS ref.{new} RENAME TO {old}")
    op.execute("ALTER TABLE ref.airline RENAME TO airlines")
    op.execute("ALTER TABLE ref.airlines SET SCHEMA api")
    op.execute("COMMENT ON SCHEMA api IS NULL")
