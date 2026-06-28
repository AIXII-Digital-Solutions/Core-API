"""make api.sync_registration_from_asg() SECURITY DEFINER

Revision ID: f1a2b3c4d5e6
Revises: e0f1a2b3c4d5
Create Date: 2026-06-28

external-worker (role svc_external_worker) calls this function after refreshing cirium.asg, but it
only has READ on schema api — it cannot TRUNCATE/INSERT api.registration itself. Running the
function as SECURITY DEFINER makes its body execute as the owner (developer, who owns all schemas),
so the worker just needs USAGE + SELECT on api (granted in docs/db-aixii-setup.sql). search_path is
pinned and every object is schema-qualified to keep the definer function safe.
"""
from alembic import op

revision = "f1a2b3c4d5e6"
down_revision = "e0f1a2b3c4d5"
branch_labels = None
depends_on = None


DEFINER_FN = """
CREATE OR REPLACE FUNCTION api.sync_registration_from_asg() RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
BEGIN
    TRUNCATE api.registration RESTART IDENTITY;
    INSERT INTO api.registration (reg, msn, airline_id)
    SELECT a."Registration", a."Serial Number", al.id
    FROM cirium.asg a
    LEFT JOIN api.airlines al ON al.airline_name = a."Airline"
    WHERE a.is_active;
END;
$$;
"""

INVOKER_FN = """
CREATE OR REPLACE FUNCTION api.sync_registration_from_asg() RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    TRUNCATE api.registration RESTART IDENTITY;
    INSERT INTO api.registration (reg, msn, airline_id)
    SELECT a."Registration", a."Serial Number", al.id
    FROM cirium.asg a
    LEFT JOIN api.airlines al ON al.airline_name = a."Airline"
    WHERE a.is_active;
END;
$$;
"""


def upgrade() -> None:
    op.execute(DEFINER_FN)


def downgrade() -> None:
    op.execute(INVOKER_FN)
