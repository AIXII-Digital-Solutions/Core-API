"""Column comments certificates_schema left out

The models declare comments on the new certificate, policy-wording and airline-code columns;
certificates_schema created the columns without them, so every autogenerate proposed to add them.
Read from the models themselves, so the two cannot drift again.

Revision ID: certificates_comments
Revises: certificates_schema
Create Date: 2026-09-29
"""
import sys
from pathlib import Path

from alembic import op

revision = "certificates_comments"
down_revision = "certificates_schema"
branch_labels = None
depends_on = None

_COLUMNS = {
    "certificate.reinsurance_certificate": ("date_of_issue_source", "variant", "issued_by"),
    "certificate.insurance_certificate": ("date_of_issue_source", "variant", "issued_by"),
    "policy.policy": ("period_wording", "geographical_limits", "hull_war_clause",
                      "war_exclusion_clause", "war_exclusion_exception", "war_liability_clause",
                      "fifty_fifty_clause"),
    "ref.airline": ("certificate_code",),
}


def _tables():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "db-contract"))
    from Database.CertificateModels import ReinsuranceCertificate, InsuranceCertificate
    from Database.PolicyModels import Policy
    from Database.RefModels import Airline
    return {t.__table__.fullname: t.__table__
            for t in (ReinsuranceCertificate, InsuranceCertificate, Policy, Airline)}


def upgrade() -> None:
    tables = _tables()
    for table, columns in _COLUMNS.items():
        for column in columns:
            comment = tables[table].c[column].comment.replace("'", "''")
            op.execute(f"COMMENT ON COLUMN {table}.{column} IS '{comment}'")


def downgrade() -> None:
    for table, columns in _COLUMNS.items():
        for column in columns:
            op.execute(f"COMMENT ON COLUMN {table}.{column} IS NULL")
