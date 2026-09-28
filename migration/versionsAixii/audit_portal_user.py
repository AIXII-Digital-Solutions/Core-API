"""The change log records WHICH PORTAL USER made a change

`audit.change_log.changed_by` names the API credential — nearly always 'service-token', because the
portal calls with it on behalf of whoever is signed in. The portal now says who that is (X-Portal-
User-Id / -Email / -Name, believed only next to the service token), the API puts them in three
transaction-local settings, and the trigger stores them:

    changed_by_user_id     the portal user's UUID
    changed_by_user_email
    changed_by_user_name

With no portal user, the change is the system's — a migration, an _admin loader, the status sync, a
developer using the service token directly — and is stored as changed_by_user_name = 'System' with
the other two null. The one exception is another API client (app.actor_kind = 'api_key'), whose
changes carry no person at all: all three null. `changed_by` is untouched.

Every entry already in the log predates this and is marked System.

The function keeps the `=` assignment and has no colon-word sequences: SQLAlchemy's text() would
read them as bind parameters (see migration/insured_fleet_sql.py).

Revision ID: audit_portal_user
Revises: policy_currency_drop_old
Create Date: 2026-09-29
"""
import importlib.util
import pathlib

from alembic import op

_spec = importlib.util.spec_from_file_location(
    "_insured_fleet_sql", pathlib.Path(__file__).parents[1] / "insured_fleet_sql.py")
_sql = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_sql)

revision = "audit_portal_user"
down_revision = "policy_currency_drop_old"
branch_labels = None
depends_on = None

_FUNCTION = """
CREATE OR REPLACE FUNCTION audit.log_change() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $fn$
DECLARE
    v_old   jsonb;
    v_new   jsonb;
    v_id    bigint;
    v_uid   text;
    v_email text;
    v_name  text;
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

    -- Who, as a person. The portal user when the API named one; otherwise System, unless the
    -- caller is another API client, which names nobody.
    v_uid = nullif(current_setting('portal.user_id', true), '');
    IF v_uid IS NOT NULL THEN
        v_email = nullif(current_setting('portal.user_email', true), '');
        v_name  = nullif(current_setting('portal.user_name', true), '');
    ELSIF coalesce(current_setting('app.actor_kind', true), '') <> 'api_key' THEN
        v_name = 'System';
    END IF;

    INSERT INTO audit.change_log
        (schema_name, table_name, row_id, operation, changed_by,
         changed_by_user_id, changed_by_user_email, changed_by_user_name, old_row, new_row)
    VALUES
        (TG_TABLE_SCHEMA, TG_TABLE_NAME, v_id, TG_OP,
         coalesce(nullif(current_setting('app.actor', true), ''), session_user),
         v_uid, v_email, v_name, v_old, v_new);

    RETURN NULL;
END;
$fn$
"""


def upgrade() -> None:
    op.execute("ALTER TABLE audit.change_log ADD COLUMN changed_by_user_id text")
    op.execute("ALTER TABLE audit.change_log ADD COLUMN changed_by_user_email text")
    op.execute("ALTER TABLE audit.change_log ADD COLUMN changed_by_user_name text")
    op.execute("CREATE INDEX ix_change_log_changed_by_user_id ON audit.change_log "
               "(changed_by_user_id) WHERE changed_by_user_id IS NOT NULL")
    op.execute("COMMENT ON COLUMN audit.change_log.changed_by_user_id IS "
               "'The portal user (UUID) the change was made for; null for System and API clients.'")
    op.execute("COMMENT ON COLUMN audit.change_log.changed_by_user_name IS "
               "'The portal user''s full name, or System for migrations, loaders, system jobs and "
               "entries recorded before users were.'")
    op.execute(_FUNCTION)
    op.execute("UPDATE audit.change_log SET changed_by_user_name = 'System' "
               "WHERE changed_by_user_id IS NULL AND changed_by_user_name IS NULL")


def downgrade() -> None:
    op.execute(_sql.AUDIT_FUNCTION)
    op.execute("DROP INDEX IF EXISTS audit.ix_change_log_changed_by_user_id")
    op.execute("ALTER TABLE audit.change_log DROP COLUMN changed_by_user_name")
    op.execute("ALTER TABLE audit.change_log DROP COLUMN changed_by_user_email")
    op.execute("ALTER TABLE audit.change_log DROP COLUMN changed_by_user_id")
