"""forecast schema grants + matview ownership so the service roles can use them

Revision ID: e9f0a1b2c3d4
Revises: d8e9f0a1b2c3
Create Date: 2026-07-02

The `forecast` schema (migration d7e8f9a0b1c2) and cirium.registrations matview (d8e9f0a1b2c3) were
created by the migration owner (`developer`) but never GRANTed to the application roles, so
svc_external_worker (member of grp_aviation_write) hit "permission denied for schema forecast" on the
first real run. This mirrors the per-schema grants in docs/db-aixii-setup.sql, with two forecast-
specific needs:

  * TRUNCATE — the pipeline TRUNCATEs forecast.future_1 / final_1 each request; TRUNCATE is a
    separate privilege NOT covered by the standard SELECT/INSERT/UPDATE/DELETE grant.
  * matview REFRESH requires OWNERSHIP — a developer-owned matview cannot be refreshed by the worker.
    Reassign cirium.registrations AND cirium.airlines to grp_aviation_write (which svc_external_worker
    is a member of) so asg_regs_updater's REFRESH works. (cirium.airlines had the same latent issue.)

Guarded by role existence so it is a no-op on a role-less local/dev database.
"""
from alembic import op

revision = "e9f0a1b2c3d4"
down_revision = "d8e9f0a1b2c3"
branch_labels = None
depends_on = None


UPGRADE_SQL = r"""
DO $do$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aviation_write') THEN
        EXECUTE 'GRANT USAGE ON SCHEMA forecast TO grp_aviation_write';
        EXECUTE 'GRANT SELECT,INSERT,UPDATE,DELETE,TRUNCATE ON ALL TABLES IN SCHEMA forecast TO grp_aviation_write';
        EXECUTE 'GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA forecast TO grp_aviation_write';
        EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA forecast GRANT SELECT,INSERT,UPDATE,DELETE,TRUNCATE ON TABLES TO grp_aviation_write';
        EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA forecast GRANT USAGE,SELECT ON SEQUENCES TO grp_aviation_write';
        -- matviews the worker must REFRESH must be owned by a role it is a member of
        EXECUTE 'ALTER MATERIALIZED VIEW cirium.registrations OWNER TO grp_aviation_write';
        EXECUTE 'ALTER MATERIALIZED VIEW cirium.airlines OWNER TO grp_aviation_write';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aixii_read') THEN
        EXECUTE 'GRANT USAGE ON SCHEMA forecast TO grp_aixii_read';
        EXECUTE 'GRANT SELECT ON ALL TABLES IN SCHEMA forecast TO grp_aixii_read';
        EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA forecast GRANT SELECT ON TABLES TO grp_aixii_read';
    END IF;
END
$do$;
"""

DOWNGRADE_SQL = r"""
DO $do$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aviation_write') THEN
        EXECUTE 'ALTER MATERIALIZED VIEW cirium.registrations OWNER TO developer';
        EXECUTE 'ALTER MATERIALIZED VIEW cirium.airlines OWNER TO developer';
        EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA forecast REVOKE SELECT,INSERT,UPDATE,DELETE,TRUNCATE ON TABLES FROM grp_aviation_write';
        EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA forecast REVOKE USAGE,SELECT ON SEQUENCES FROM grp_aviation_write';
        EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA forecast FROM grp_aviation_write';
        EXECUTE 'REVOKE ALL ON ALL SEQUENCES IN SCHEMA forecast FROM grp_aviation_write';
        EXECUTE 'REVOKE USAGE ON SCHEMA forecast FROM grp_aviation_write';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aixii_read') THEN
        EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA forecast REVOKE SELECT ON TABLES FROM grp_aixii_read';
        EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA forecast FROM grp_aixii_read';
        EXECUTE 'REVOKE USAGE ON SCHEMA forecast FROM grp_aixii_read';
    END IF;
END
$do$;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
