"""Raw SQL for the `insured_fleet_rebuild` revision — the parts Alembic cannot autogenerate.

Kept beside the revision rather than inside it so the statements can be read on their own and
re-used by a later revision that needs to re-create one of them. Alembic imports a revision as a
STANDALONE module, so the revision loads this file by path (importlib), not by package import.

Every statement here obeys the two rules this repo keeps tripping over:
  * one statement per op.execute() — asyncpg rejects multiple statements in one call, so a function
    and its trigger are two calls;
  * no `:word` sequences anywhere, INCLUDING inside SQL comments, because SQLAlchemy's text() reads
    them as bind parameters. PL/pgSQL assignment is written `=`, never `:=`, for the same reason.
"""

# --- the schemas, in dependency order ---------------------------------------------------------
SCHEMAS = ("ref", "fleet", "leasing", "policy", "audit")
# The four that hold business data and are written through the API. `audit` is not one of
# them: it is written only by the trigger, which runs as its definer.
DATA_SCHEMAS = ("ref", "fleet", "leasing", "policy")

# Every table that carries the audit trigger. `api.airlines` is included because the airline is
# part of the insured-aircraft record (group 7 of the specification) even though the table itself
# belongs to another domain.
AUDITED = (
    ("ref", "party"),
    ("ref", "party_contact"),
    ("fleet", "aircraft_type"),
    ("fleet", "aircraft"),
    ("fleet", "aircraft_engine"),
    ("leasing", "agreement"),
    ("leasing", "aircraft_lease"),
    ("policy", "policy"),
    ("policy", "coverage"),
    ("api", "airlines"),
)

# Roles, as provisioned by docs/db-aixii-setup.sql. Mirrors what the dropped `insurance` schema had.
READ_ROLES = ("grp_aixii_read", "grp_aviation_write", "svc_external_worker")
WRITE_ROLE = "grp_api_write"


# --- audit ------------------------------------------------------------------------------------

AUDIT_FUNCTION = """
CREATE OR REPLACE FUNCTION audit.log_change() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $fn$
DECLARE
    v_old jsonb;
    v_new jsonb;
    v_id  bigint;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        v_old = to_jsonb(OLD);
    END IF;
    IF TG_OP <> 'DELETE' THEN
        v_new = to_jsonb(NEW);
    END IF;

    -- A save that moved nothing but the row timestamp is not a change worth a log row.
    IF TG_OP = 'UPDATE' AND (v_old - 'updated_at') = (v_new - 'updated_at') THEN
        RETURN NULL;
    END IF;

    v_id = coalesce((v_new ->> 'id')::bigint, (v_old ->> 'id')::bigint);

    INSERT INTO audit.change_log
        (schema_name, table_name, row_id, operation, changed_by, old_row, new_row)
    VALUES
        (TG_TABLE_SCHEMA, TG_TABLE_NAME, v_id, TG_OP,
         coalesce(nullif(current_setting('app.actor', true), ''), session_user),
         v_old, v_new);

    RETURN NULL;
END;
$fn$
"""

AUDIT_FUNCTION_COMMENT = (
    "COMMENT ON FUNCTION audit.log_change() IS "
    "'AFTER trigger for every table in ref/fleet/leasing/policy and api.airlines. Writes one "
    "whole-row snapshot pair per change into audit.change_log, attributed to the app.actor GUC "
    "when the API sets it. SECURITY DEFINER so a role that may write the table but not the log "
    "still produces an audit row.'"
)


def audit_trigger(schema: str, table: str) -> str:
    """The trigger that hangs `audit.log_change()` off one table."""
    return (
        f"CREATE TRIGGER {table}_audit "
        f"AFTER INSERT OR UPDATE OR DELETE ON {schema}.{table} "
        f"FOR EACH ROW EXECUTE FUNCTION audit.log_change()"
    )


def drop_audit_trigger(schema: str, table: str) -> str:
    return f"DROP TRIGGER IF EXISTS {table}_audit ON {schema}.{table}"


# --- the agreed-value calculation ---------------------------------------------------------------
# leasing.aircraft_lease.agreed_value_final STORES what the schedule stated. This recomputes it from
# the same inputs so the read layer can show both and flag a divergence, and so a report can age a
# value to any date without pulling the arithmetic into Python.
#
# Compounding on WHOLE years elapsed: the value steps once a year, on the anniversary of the
# depreciation start date, which is how the schedules are written. Straight-line would be
#   preliminary * (1 - ratio/100 * years)
# — change THIS FUNCTION, not the column, if that is what the business means.

AGREED_VALUE_FUNCTION = """
CREATE OR REPLACE FUNCTION leasing.agreed_value_at(
    preliminary numeric,
    ratio       numeric,
    start_date  date,
    fixed       boolean,
    on_date     date
) RETURNS numeric
LANGUAGE sql IMMUTABLE AS $fn$
    SELECT CASE
        WHEN preliminary IS NULL THEN NULL
        WHEN coalesce(fixed, false) THEN preliminary
        WHEN ratio IS NULL OR start_date IS NULL OR on_date IS NULL THEN preliminary
        WHEN on_date <= start_date THEN preliminary
        ELSE round(
            preliminary
            * power(1 - ratio / 100.0,
                    floor(extract(epoch FROM age(on_date, start_date)) / 31556952.0)),
            2)
    END
$fn$
"""

AGREED_VALUE_COMMENT = (
    "COMMENT ON FUNCTION leasing.agreed_value_at(numeric, numeric, date, boolean, date) IS "
    "'Agreed value depreciated to a date. Compounding on whole years elapsed since the "
    "depreciation start; returns the preliminary value unchanged when agreed_value_fixed is true "
    "or an input is missing. leasing.aircraft_lease.agreed_value_final holds the figure the "
    "schedule STATED - compare, never overwrite.'"
)


# --- one aircraft holds one policy at a time ----------------------------------------------------
# Raw SQL, not metadata: Alembic autogenerate neither emits nor understands exclusion constraints,
# so keeping this out of the model is what stops a future autogenerate proposing to drop it.

COVERAGE_EXCLUSION = """
ALTER TABLE policy.coverage ADD CONSTRAINT ex_coverage_no_overlap
EXCLUDE USING gist (
    aircraft_id WITH =,
    daterange(covered_from, covered_to, '[]') WITH &&
)
"""

COVERAGE_EXCLUSION_COMMENT = (
    "COMMENT ON CONSTRAINT ex_coverage_no_overlap ON policy.coverage IS "
    "'An aircraft is covered by at most one policy on any given day. The range is inclusive at "
    "both ends, so annual policies meeting on 31 Dec / 1 Jan do not collide. DROP THIS if the "
    "business ever layers concurrent contracts (a separate war-risk policy alongside all-risks) - "
    "nothing else in the schema depends on it.'"
)


# --- grants -------------------------------------------------------------------------------------

def grants(schema: str) -> list[str]:
    """Replicates what the dropped `insurance` schema had, per DATA schema, one statement
    per call (asyncpg rejects multiple statements in one op.execute)."""
    read = ", ".join(READ_ROLES)
    out = [
        f"GRANT USAGE ON SCHEMA {schema} TO {read}, {WRITE_ROLE}",
        f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {read}",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {schema} TO {WRITE_ROLE}",
        f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {schema} TO {WRITE_ROLE}",
        # so a table added by a later migration is reachable without repeating the block
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} GRANT SELECT ON TABLES TO {read}",
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {WRITE_ROLE}",
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} GRANT USAGE, SELECT ON SEQUENCES TO {WRITE_ROLE}",
    ]
    return out


# audit.change_log is written by the trigger, which is SECURITY DEFINER — so nobody else needs
# INSERT on it, and giving the API DELETE on its own audit trail would defeat the point. Read only.
AUDIT_GRANTS = (
    f"GRANT USAGE ON SCHEMA audit TO {', '.join(READ_ROLES)}, {WRITE_ROLE}",
    f"GRANT SELECT ON ALL TABLES IN SCHEMA audit TO {', '.join(READ_ROLES)}, {WRITE_ROLE}",
    f"ALTER DEFAULT PRIVILEGES IN SCHEMA audit GRANT SELECT ON TABLES TO "
    f"{', '.join(READ_ROLES)}, {WRITE_ROLE}",
)
