"""Certificates: review pipeline and electronic signature

draft -> in_review -> approved -> issued. A second person approves what a first one submitted, and
the certificate is issued by uploading the PDF signed electronically (the bytes are kept as signed).

New on both certificate tables:
  created_by_user_email
  submitted_by_user (jsonb {id, email, name}), submitted_at
  approved_by_user, approved_at, approved_hash (sha256 of the values approved)
  returned (jsonb, the last return to draft), events (jsonb, the trail, oldest first)
  issue_prep (jsonb: token hash, expiry, sha256 + length of the prepared PDF, its values)
  signature (jsonb: the signature as the portal reported it + sha256 of the stored file)

The status check takes the two new values, and "issued has its PDF" no longer assumes that every
other status is draft. The freeze trigger is unchanged: only `issued` is final.

Revision ID: certificate_review
Revises: certificate_wording
Create Date: 2026-10-01
"""
from alembic import op

revision = "certificate_review"
down_revision = "certificate_wording"
branch_labels = None
depends_on = None

_TABLES = ("reinsurance_certificate", "insurance_certificate")
_COLUMNS = (
    ("created_by_user_email", "varchar", None),
    ("submitted_by_user", "jsonb", None),
    ("submitted_at", "timestamptz", None),
    ("approved_by_user", "jsonb", None),
    ("approved_at", "timestamptz", None),
    ("approved_hash", "varchar(64)",
     "sha256 of the canonical JSON of the values approved; issuing re-checks it."),
    ("returned", "jsonb", "The last return to draft: {by_user, at, comment, from_status}; cleared on submit."),
    ("events", "jsonb NOT NULL DEFAULT '[]'::jsonb",
     "[{action, by_user, at, comment}] oldest first: created/submitted/approved/returned/issued."),
    ("issue_prep", "jsonb",
     "The PDF prepared for signing: token hash, expiry, sha256 + length of its bytes, the values and "
     "signatory it was drawn with."),
    ("signature", "jsonb",
     "The signature of the issued PDF as the portal reported it, + sha256 of the stored file."),
)


def upgrade() -> None:
    for t in _TABLES:
        for column, kind, comment in _COLUMNS:
            op.execute(f"ALTER TABLE certificate.{t} ADD COLUMN {column} {kind}")
            if comment:
                op.execute(f"COMMENT ON COLUMN certificate.{t}.{column} IS '{comment}'")
        op.execute(f"ALTER TABLE certificate.{t} DROP CONSTRAINT ck_{t}_status")
        op.execute(f"ALTER TABLE certificate.{t} ADD CONSTRAINT ck_{t}_status "
                   f"CHECK (status IN ('draft', 'in_review', 'approved', 'issued'))")
        op.execute(f"ALTER TABLE certificate.{t} DROP CONSTRAINT ck_{t}_issued_complete")
        op.execute(f"ALTER TABLE certificate.{t} ADD CONSTRAINT ck_{t}_issued_complete "
                   f"CHECK (status <> 'issued' OR (pdf IS NOT NULL AND issued_at IS NOT NULL))")
        op.execute(f"COMMENT ON COLUMN certificate.{t}.status IS "
                   f"'draft -> in_review -> approved -> issued (frozen). Only a draft is editable.'")


def downgrade() -> None:
    for t in _TABLES:
        op.execute(f"UPDATE certificate.{t} SET status = 'draft' WHERE status IN ('in_review', 'approved')")
        op.execute(f"ALTER TABLE certificate.{t} DROP CONSTRAINT ck_{t}_issued_complete")
        op.execute(f"ALTER TABLE certificate.{t} ADD CONSTRAINT ck_{t}_issued_complete "
                   f"CHECK (status = 'draft' OR (pdf IS NOT NULL AND issued_at IS NOT NULL))")
        op.execute(f"ALTER TABLE certificate.{t} DROP CONSTRAINT ck_{t}_status")
        op.execute(f"ALTER TABLE certificate.{t} ADD CONSTRAINT ck_{t}_status "
                   f"CHECK (status IN ('draft', 'issued'))")
        for column, _kind, _comment in reversed(_COLUMNS):
            op.execute(f"ALTER TABLE certificate.{t} DROP COLUMN {column}")
