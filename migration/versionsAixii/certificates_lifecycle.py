"""Certificates are drafts until issued; the issuing user signs by hand

1. DRAFT -> ISSUED. A certificate is now created as a `draft`: its inputs (`request`) can still
   change, its values are re-resolved whenever it is read, every PDF of it is marked DRAFT. Issuing
   it freezes `data`, `alerts` and the PDF, stamps `issued_at` and the issuer — and from then on the
   trigger `certificate.freeze_issued()` refuses any UPDATE or DELETE of the row. The API role gets
   UPDATE / DELETE for drafts; the trigger, not the grant, is what protects an issued certificate.

   New on both tables: status, request, errors, issued_at, created_by_user_id / _name. `pdf` becomes
   nullable (a draft has none stored) with a CHECK that an issued row has its PDF and issued_at.

2. NO SIGNATURE ON FILE. The issuing user signs the printed certificate by hand; the portal sends
   their name, title, phone and e-mail with the request. certificate.signatory (the per-user profile)
   and the uploaded signature images go. The company stamp stays, and is printed only when one has
   been uploaded.

Both certificate tables are empty at this revision, so no row needs converting.

Revision ID: certificates_lifecycle
Revises: certificates_settings
Create Date: 2026-09-30
"""
from alembic import op

revision = "certificates_lifecycle"
down_revision = "certificates_settings"
branch_labels = None
depends_on = None

_TABLES = ("reinsurance_certificate", "insurance_certificate")
_WRITE_ROLE = "grp_api_write"

# The one change an issued row accepts is its links being cleared: aircraft_id / policy_id /
# aircraft_lease_id are ON DELETE SET NULL, which Postgres carries out as an UPDATE of this row, and
# refusing it would make the aircraft, policy or lease undeletable for as long as a certificate exists.
_FREEZE = """
CREATE OR REPLACE FUNCTION certificate.freeze_issued() RETURNS trigger
LANGUAGE plpgsql AS $fn$
DECLARE
    v_links text[] = ARRAY['aircraft_id', 'policy_id', 'aircraft_lease_id', 'updated_at'];
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.status = 'issued'
       AND (to_jsonb(NEW) - v_links) = (to_jsonb(OLD) - v_links)
       AND (NEW.aircraft_id IS NULL OR NEW.aircraft_id = OLD.aircraft_id)
       AND (NEW.policy_id IS NULL OR NEW.policy_id = OLD.policy_id)
       AND (NEW.aircraft_lease_id IS NULL OR NEW.aircraft_lease_id = OLD.aircraft_lease_id) THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'issued' THEN
        RAISE EXCEPTION 'certificate % is issued and cannot be changed', OLD.reference_number
            USING ERRCODE = 'check_violation', CONSTRAINT = 'certificate_issued_is_final';
    END IF;
    RETURN CASE TG_OP WHEN 'DELETE' THEN OLD ELSE NEW END;
END;
$fn$
"""


def upgrade() -> None:
    op.execute(_FREEZE)
    for t in _TABLES:
        op.execute(f"ALTER TABLE certificate.{t} ADD COLUMN status varchar NOT NULL DEFAULT 'draft'")
        op.execute(f"ALTER TABLE certificate.{t} ADD COLUMN request jsonb NOT NULL DEFAULT '{{}}'::jsonb")
        op.execute(f"ALTER TABLE certificate.{t} ADD COLUMN errors jsonb NOT NULL DEFAULT '[]'::jsonb")
        op.execute(f"ALTER TABLE certificate.{t} ADD COLUMN issued_at timestamptz")
        op.execute(f"ALTER TABLE certificate.{t} ADD COLUMN created_by_user_id varchar")
        op.execute(f"ALTER TABLE certificate.{t} ADD COLUMN created_by_user_name varchar")
        op.execute(f"ALTER TABLE certificate.{t} ALTER COLUMN pdf DROP NOT NULL")
        op.execute(f"ALTER TABLE certificate.{t} ADD CONSTRAINT ck_{t}_status "
                   f"CHECK (status IN ('draft', 'issued'))")
        op.execute(f"ALTER TABLE certificate.{t} ADD CONSTRAINT ck_{t}_issued_complete "
                   f"CHECK (status = 'draft' OR (pdf IS NOT NULL AND issued_at IS NOT NULL))")
        op.execute(f"COMMENT ON COLUMN certificate.{t}.status IS "
                   f"'draft (still editable, drawn marked DRAFT) or issued (frozen).'")
        op.execute(f"COMMENT ON COLUMN certificate.{t}.request IS "
                   f"'The inputs: policy, date of issue, overrides, signatory — what a draft edit changes.'")
        op.execute(f"COMMENT ON COLUMN certificate.{t}.errors IS "
                   f"'What still stops a draft being issued; always empty once issued.'")
        what = t.split("_")[0]
        op.execute(f"COMMENT ON TABLE certificate.{t} IS 'Every {what} certificate, from draft to "
                   f"issued. An issued row is frozen (trigger): its values, alerts and the PDF as sent.'")
        op.execute(f"CREATE TRIGGER {t}_freeze BEFORE UPDATE OR DELETE ON certificate.{t} "
                   f"FOR EACH ROW EXECUTE FUNCTION certificate.freeze_issued()")
        op.execute(f"GRANT UPDATE, DELETE ON certificate.{t} TO {_WRITE_ROLE}")

    op.execute("DROP TABLE certificate.signatory")
    op.execute("DELETE FROM certificate.asset WHERE key LIKE 'signature:%'")


def downgrade() -> None:
    op.execute("""
        CREATE TABLE certificate.signatory (
            id              bigserial PRIMARY KEY,
            portal_user_id  varchar NOT NULL,
            name            varchar,
            email           varchar,
            title           varchar,
            phone           varchar,
            created_at      timestamp NOT NULL DEFAULT now(),
            updated_at      timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_certificate_signatory_portal_user UNIQUE (portal_user_id)
        )
    """)
    for t in _TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {t}_freeze ON certificate.{t}")
        op.execute(f"REVOKE UPDATE, DELETE ON certificate.{t} FROM {_WRITE_ROLE}")
        op.execute(f"DELETE FROM certificate.{t} WHERE status = 'draft'")
        op.execute(f"ALTER TABLE certificate.{t} DROP CONSTRAINT ck_{t}_issued_complete")
        op.execute(f"ALTER TABLE certificate.{t} DROP CONSTRAINT ck_{t}_status")
        op.execute(f"ALTER TABLE certificate.{t} ALTER COLUMN pdf SET NOT NULL")
        for c in ("status", "request", "errors", "issued_at", "created_by_user_id",
                  "created_by_user_name"):
            op.execute(f"ALTER TABLE certificate.{t} DROP COLUMN {c}")
    op.execute("DROP FUNCTION IF EXISTS certificate.freeze_issued()")
