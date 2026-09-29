"""Certificate branding and signatories live in the database, edited from the portal

The company the certificates are issued by (names, address line, legal footer, colours), its images
(logo, stamp) and each issuing user's signatory details (title, phone, signature) were constants in
app/Certificates/branding.py. They become data:

    certificate.settings    ONE row (id = 1) — the company as printed. Audited.
    certificate.signatory   one row per portal user who issues certificates — what the portal does
                            not send with the request: title, phone, optional name/e-mail
                            overrides. Audited.
    certificate.asset       the images: `logo`, `stamp`, `signature:<portal user id>`; PNG, JPEG or
                            SVG. NOT audited — a change-log entry per upload would copy the bytes.

Seeded with legal name AI12 (no header text — the logo says it) and the AI12 logo (app/Certificates/assets/default_logo.svg); the
legal name, address line and footer are filled in from the portal.

Revision ID: certificates_settings
Revises: certificates_comments
Create Date: 2026-09-29
"""
import hashlib
from pathlib import Path

from alembic import op
from sqlalchemy import text

revision = "certificates_settings"
down_revision = "certificates_comments"
branch_labels = None
depends_on = None

_READ_ROLES = "grp_aixii_read, grp_aviation_write, svc_external_worker"
_WRITE_ROLE = "grp_api_write"
_LOGO = Path(__file__).resolve().parents[2] / "app" / "Certificates" / "assets" / "default_logo.svg"

_COMMENTS = {
    ("settings", "company_name"): "The name beside the logo in the page header; empty = logo only.",
    ("settings", "company_legal_name"): "As held on file by … / AUTHORISED SIGNATORY …",
    ("settings", "address_line"): "The address / phone / website line at the foot of page 1.",
    ("settings", "legal_footer"): "The regulatory small print at the foot of page 1.",
    ("settings", "brand_primary"): "Header text colour, #RRGGBB.",
    ("settings", "brand_accent"): "The rule above the page number, #RRGGBB.",
    ("signatory", "name"): "Overrides the portal name on the certificate when set.",
    ("signatory", "email"): "Overrides the portal e-mail on the certificate when set.",
}


def upgrade() -> None:
    op.execute("""
        CREATE TABLE certificate.settings (
            id                  bigint PRIMARY KEY DEFAULT 1,
            company_name        varchar,
            company_legal_name  varchar NOT NULL,
            address_line        varchar,
            legal_footer        text,
            brand_primary       varchar(7) NOT NULL DEFAULT '#1F3B33',
            brand_accent        varchar(7) NOT NULL DEFAULT '#D5E28D',
            created_at          timestamp NOT NULL DEFAULT now(),
            updated_at          timestamp NOT NULL DEFAULT now(),
            CONSTRAINT ck_certificate_settings_single_row CHECK (id = 1),
            CONSTRAINT ck_certificate_settings_colours
                CHECK (brand_primary ~ '^#[0-9A-Fa-f]{6}$' AND brand_accent ~ '^#[0-9A-Fa-f]{6}$')
        )
    """)
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
    op.execute("""
        CREATE TABLE certificate.asset (
            id                  bigserial PRIMARY KEY,
            key                 varchar NOT NULL,
            content_type        varchar NOT NULL,
            data                bytea NOT NULL,
            sha256              varchar(64) NOT NULL,
            size                integer NOT NULL,
            updated_by          varchar,
            updated_by_user_id  varchar,
            created_at          timestamp NOT NULL DEFAULT now(),
            updated_at          timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_certificate_asset_key UNIQUE (key),
            CONSTRAINT ck_certificate_asset_content_type
                CHECK (content_type IN ('image/png', 'image/jpeg', 'image/svg+xml'))
        )
    """)
    for (table, column), comment in _COMMENTS.items():
        op.execute(f"COMMENT ON COLUMN certificate.{table}.{column} IS '{comment}'")
    for table in ("settings", "signatory"):
        op.execute(f"CREATE TRIGGER {table}_audit AFTER INSERT OR UPDATE OR DELETE "
                   f"ON certificate.{table} FOR EACH ROW EXECUTE FUNCTION audit.log_change()")
    for table in ("settings", "signatory", "asset"):
        op.execute(f"GRANT SELECT ON certificate.{table} TO {_READ_ROLES}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON certificate.settings TO {_WRITE_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON certificate.signatory TO {_WRITE_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON certificate.asset TO {_WRITE_ROLE}")
    for seq in ("signatory_id_seq", "asset_id_seq"):
        op.execute(f"GRANT USAGE, SELECT ON SEQUENCE certificate.{seq} TO {_WRITE_ROLE}")

    op.execute("SELECT set_config('app.actor', 'migration certificates_settings', true)")
    # no company_name: the AI12 logo already says it; the header shows the logo alone
    op.execute("INSERT INTO certificate.settings (id, company_legal_name) VALUES (1, 'AI12')")
    logo = _LOGO.read_bytes()
    op.get_bind().execute(
        text("INSERT INTO certificate.asset (key, content_type, data, sha256, size, updated_by) "
             "VALUES ('logo', 'image/svg+xml', :data, :sha, :size, 'migration certificates_settings')"),
        {"data": logo, "sha": hashlib.sha256(logo).hexdigest(), "size": len(logo)})


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS certificate.asset")
    op.execute("DROP TABLE IF EXISTS certificate.signatory")
    op.execute("DROP TABLE IF EXISTS certificate.settings")
