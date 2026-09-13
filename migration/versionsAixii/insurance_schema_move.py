"""move the aircraft-insurance domain out of `api` into its own `insurance` schema

Revision ID: insurance_schema_move
Revises: forecast_wet_sentinel_grouped
Create Date: 2026-09-10

The insurance domain was created inside `api` (ad60f0b27298 + fa38ae0ab542) because `api.airlines`
was already there. It has since grown to eleven tables, three enums and two audit triggers — one
business domain, so it gets one schema, like every aviation source has.

WHAT MOVES: the eleven tables with their indexes, constraints and owned id sequences (all carried
along by ALTER TABLE ... SET SCHEMA), the three native enums, and both audit trigger functions.

WHAT STAYS in `api`: `airlines` and `registration`. `airlines` is not an insurance table — the
cirium asg sync resolves names against it, api.registration has a FK to it, and grp_aviation_write
reads it during matview refreshes. The insurance tables keep pointing at it ACROSS schemas; a
cross-schema foreign key is ordinary in PostgreSQL and needs no change here.

NO DATA IS COPIED. SET SCHEMA is a catalogue update: the rows, the ids, the sequence positions and
the table-level grants all stay as they are. Nothing outside this domain referenced these tables
(no view, matview or function did), so there is nothing to re-point.

THE INDEX RENAME is not cosmetic. SQLAlchemy derives an implicit index name from the table's schema
(`index=True` on insurance_claims.aircraft_id yields ix_insurance_insurance_claims_aircraft_id once
the table lives in `insurance`), while SET SCHEMA leaves the physical name as ix_api_... . Without
the rename every autogenerate run would propose dropping and recreating ~25 indexes forever. The
loop renames whatever `ix_api_%` it finds in the new schema, so it needs no hand-kept list.

THE TRIGGERS ARE RECREATED, not moved. A trigger keeps pointing at its function by OID, so moving
the function alone would leave the audit firing — but its BODY writes to `api.insurance_*_history`
by name, and those tables no longer exist under that name. Hence: create the functions in
`insurance` writing to `insurance.*`, re-point the triggers at them, drop the old ones.

Models: db-contract/Database/InsuranceModels.py (new file, split out of ApiModels.py; runtime copy
app/Database/InsuranceModels.py). Docs: docs/insurance.md.

asyncpg prepares every statement, so ONE op.execute() = ONE statement (a function and its trigger
are separate calls), and the SQL carries no `:word` sequences — sa.text() would read them as binds.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'insurance_schema_move'
down_revision: Union[str, Sequence[str], None] = 'forecast_wet_sentinel_grouped'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Order is irrelevant to SET SCHEMA (a foreign key follows its table, it is not re-resolved by
# name), but keeping the layered order makes the log readable: reference -> airframe -> insurance.
_TABLES = (
    "parties", "engine_types", "aircraft_types",
    "aircrafts", "aircraft_specs", "aircraft_engines",
    "insurance_policies", "insurance_records", "insurance_record_history",
    "insurance_claims", "insurance_claim_history",
)

_ENUMS = ("insurance_status", "insurance_source", "claim_damage_type")


def _rename_indexes(schema: str, old_prefix: str, new_prefix: str) -> str:
    """Rewrite the auto-generated `ix_<schema>_...` index names after the tables changed schema.
    Driven off the catalogue rather than a hand-kept list — an index added later with index=True
    would silently miss such a list, and the mismatch only ever shows up as permanent autogenerate
    noise."""
    return """
DO $$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT indexname FROM pg_indexes
    WHERE schemaname = '%(schema)s' AND left(indexname, %(oldlen)s) = '%(old)s'
  LOOP
    EXECUTE format('ALTER INDEX %(schema)s.%%I RENAME TO %%I',
                   r.indexname, '%(new)s' || substr(r.indexname, %(oldlen)s + 1));
  END LOOP;
END $$;
""" % {"schema": schema, "old": old_prefix, "new": new_prefix, "oldlen": len(old_prefix)}


def _grants(schema: str) -> str:
    """Schema-level USAGE plus default privileges for whatever is created here later. The
    table-level grants themselves travelled with the tables, but they are re-issued so that this
    revision on its own is enough to bring a hand-restored database to the expected state."""
    return """
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_api_write') THEN
    GRANT USAGE ON SCHEMA %(schema)s TO grp_api_write;
    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA %(schema)s TO grp_api_write;
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA %(schema)s TO grp_api_write;
    ALTER DEFAULT PRIVILEGES IN SCHEMA %(schema)s
      GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO grp_api_write;
    ALTER DEFAULT PRIVILEGES IN SCHEMA %(schema)s
      GRANT USAGE, SELECT ON SEQUENCES TO grp_api_write;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aviation_write') THEN
    GRANT USAGE ON SCHEMA %(schema)s TO grp_aviation_write;
    GRANT SELECT ON ALL TABLES IN SCHEMA %(schema)s TO grp_aviation_write;
    ALTER DEFAULT PRIVILEGES IN SCHEMA %(schema)s GRANT SELECT ON TABLES TO grp_aviation_write;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aixii_read') THEN
    GRANT USAGE ON SCHEMA %(schema)s TO grp_aixii_read;
    GRANT SELECT ON ALL TABLES IN SCHEMA %(schema)s TO grp_aixii_read;
    ALTER DEFAULT PRIVILEGES IN SCHEMA %(schema)s GRANT SELECT ON TABLES TO grp_aixii_read;
  END IF;
END $$;
""" % {"schema": schema}


# The two audit functions, parametrised by the schema that holds the tables. Everything else is
# verbatim from ad60f0b27298 / fa38ae0ab542 — this revision changes WHERE they write, nothing about
# WHAT they write.
_RECORDS_FN = """
CREATE OR REPLACE FUNCTION %(schema)s.insurance_records_audit() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO %(schema)s.insurance_record_history
        (record_id, operation, changed_at, changed_by, old_row, new_row)
    VALUES (
        OLD.id,
        TG_OP,
        now(),
        -- the API sets this GUC per transaction via set_config('app.actor', <token name>, true).
        -- Falls back to the DB login when a change is made outside the API (psql, a loader).
        COALESCE(NULLIF(current_setting('app.actor', true), ''), session_user),
        to_jsonb(OLD),
        CASE WHEN TG_OP = 'UPDATE' THEN to_jsonb(NEW) ELSE NULL END
    );
    RETURN NULL;   -- AFTER trigger, return value is ignored
END;
$$;
"""

_CLAIMS_FN = """
CREATE OR REPLACE FUNCTION %(schema)s.insurance_claims_audit() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    -- the API sets this GUC per transaction via set_config('app.actor', <token name>, true).
    -- Falls back to the DB login when a claim is touched outside the API (psql, a loader).
    actor text := COALESCE(NULLIF(current_setting('app.actor', true), ''), session_user);
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO %(schema)s.insurance_claim_history
            (claim_id, operation, changed_at, changed_by, old_row, new_row)
        VALUES (NEW.id, TG_OP, now(), actor, NULL, to_jsonb(NEW));
    ELSIF TG_OP = 'UPDATE' THEN
        INSERT INTO %(schema)s.insurance_claim_history
            (claim_id, operation, changed_at, changed_by, old_row, new_row)
        VALUES (NEW.id, TG_OP, now(), actor, to_jsonb(OLD), to_jsonb(NEW));
    ELSE
        INSERT INTO %(schema)s.insurance_claim_history
            (claim_id, operation, changed_at, changed_by, old_row, new_row)
        VALUES (OLD.id, TG_OP, now(), actor, to_jsonb(OLD), NULL);
    END IF;
    RETURN NULL;   -- AFTER trigger, return value is ignored
END;
$$;
"""

_RECORDS_TRIGGER = """
CREATE TRIGGER trg_insurance_records_audit
AFTER UPDATE OR DELETE ON %(schema)s.insurance_records
FOR EACH ROW EXECUTE FUNCTION %(schema)s.insurance_records_audit();
"""

_CLAIMS_TRIGGER = """
CREATE TRIGGER trg_insurance_claims_audit
AFTER INSERT OR UPDATE OR DELETE ON %(schema)s.insurance_claims
FOR EACH ROW EXECUTE FUNCTION %(schema)s.insurance_claims_audit();
"""


def _move(src: str, dst: str) -> None:
    """Move the whole domain from schema `src` to schema `dst`, triggers and all. upgrade() and
    downgrade() are the same operation in opposite directions, so they share this."""
    op.execute('CREATE SCHEMA IF NOT EXISTS "%s"' % dst)

    # The triggers go first: their functions are about to be replaced, and an audit that fired in
    # between would write to a history table that has already moved.
    op.execute(f"DROP TRIGGER IF EXISTS trg_insurance_records_audit ON {src}.insurance_records")
    op.execute(f"DROP TRIGGER IF EXISTS trg_insurance_claims_audit ON {src}.insurance_claims")

    for enum in _ENUMS:
        op.execute(f"ALTER TYPE {src}.{enum} SET SCHEMA {dst}")

    for table in _TABLES:
        op.execute(f"ALTER TABLE {src}.{table} SET SCHEMA {dst}")

    op.execute(_rename_indexes(dst, f"ix_{src}_", f"ix_{dst}_"))

    op.execute(_RECORDS_FN % {"schema": dst})
    op.execute(_CLAIMS_FN % {"schema": dst})
    op.execute(_RECORDS_TRIGGER % {"schema": dst})
    op.execute(_CLAIMS_TRIGGER % {"schema": dst})
    op.execute(f"DROP FUNCTION IF EXISTS {src}.insurance_records_audit()")
    op.execute(f"DROP FUNCTION IF EXISTS {src}.insurance_claims_audit()")

    op.execute(_grants(dst))


def upgrade() -> None:
    _move("api", "insurance")


def downgrade() -> None:
    # Symmetric: everything goes back to `api`, index names included, and the emptied `insurance`
    # schema is dropped with RESTRICT (the default) rather than CASCADE — if something else was
    # created in it meanwhile, the downgrade should stop and be looked at, not delete it.
    _move("insurance", "api")
    op.execute('DROP SCHEMA IF EXISTS "insurance" RESTRICT')
