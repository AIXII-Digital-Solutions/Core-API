"""grant api write on forecast.acys_claims

Revision ID: b7c1f4a92de3
Revises: 0e6444986e31
Create Date: 2026-09-08

forecast.acys_claims is the FIRST table in schema `forecast` that Core-API writes. Everything else
there is produced by external-worker's ACYS panel, so the schema's default privileges hand DML to
grp_aviation_write (the workers) and SELECT to grp_aixii_read (BI, and svc_api by membership). That
made the new table readable by the API but not writable by it, and /forecast/claims failed in
production with "permission denied for table acys_claims" on the first INSERT — while the same code
passed against a developer connection, which owns the table and never consults a grant.

Granted to grp_api_write (svc_api's write bundle), NOT to svc_api directly — privileges belong to
group roles, per docs/db-grant-access.md §0. Three grants are needed and missing any one of them
still reads as "permission denied":

    USAGE ON SCHEMA forecast     — grp_api_write had none at all; svc_api only reached the schema
                                   through grp_aixii_read
    DML ON the table             — SELECT/INSERT/UPDATE/DELETE
    USAGE, SELECT ON the sequence — id is a BIGSERIAL, so an INSERT calls nextval() and fails
                                   without it even when the table grant is right

Deliberately NOT done: `ALTER DEFAULT PRIVILEGES ... IN SCHEMA forecast ... TO grp_api_write`. That
would make every FUTURE table in the schema writable by the API, including the panel's own
acys_actuals / acys_summary_by_day, which only the worker may write. This grant is scoped to the one
table the API owns; a second such table repeats these three lines, which is the intended friction.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b7c1f4a92de3'
down_revision: Union[str, Sequence[str], None] = '0e6444986e31'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Guarded on the role existing: a developer database restored without the cluster's group roles
    # (they are provisioned by docs/db-aixii-setup.sql, outside Alembic) must still migrate.
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_api_write') THEN
                GRANT USAGE ON SCHEMA forecast TO grp_api_write;
                GRANT SELECT, INSERT, UPDATE, DELETE ON forecast.acys_claims TO grp_api_write;
                GRANT USAGE, SELECT ON SEQUENCE forecast.acys_claims_id_seq TO grp_api_write;
            END IF;
        END $$;
    """)


def downgrade() -> None:
    """Downgrade schema."""
    # USAGE ON SCHEMA is left in place: it is not ours to take back — revoking it would be a guess
    # about what else may have come to depend on grp_api_write reaching this schema. Only the two
    # object grants this revision made are withdrawn.
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_api_write') THEN
                REVOKE SELECT, INSERT, UPDATE, DELETE ON forecast.acys_claims FROM grp_api_write;
                REVOKE USAGE, SELECT ON SEQUENCE forecast.acys_claims_id_seq FROM grp_api_write;
            END IF;
        END $$;
    """)
