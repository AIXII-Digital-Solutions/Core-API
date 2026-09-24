"""Give the asg / non-asg matviews back to the worker role, so cron_asg_regs can refresh them again

`cron_asg_regs` (External-Worker, RegsListUpdater) has failed since 2026-09-21 with
"permission denied for materialized view asg_commercial". REFRESH MATERIALIZED VIEW is an OWNER
privilege — no grant stands in for it — and the five matviews it refreshes were recreated that day
by the airline-reference move (airlines_to_ref / asg_split_insured_fleet) under the migrating login,
`developer`, instead of `grp_aviation_write`, which owns every other matview the workers refresh.

Two things are needed, and the refresh fails without either:

  OWNER TO grp_aviation_write          the refresh itself
  SELECT ON api.registration           a refresh runs the query AS THE OWNER, and the non_asg_insured_*
                                       pair reads the hand-listed registrations, which the worker
                                       role could never read (verified under svc_external_worker)

ANY FUTURE REBUILD of these matviews must end with the same ALTER ... OWNER, or this comes back.

Revision ID: asg_matviews_owner
Revises: sheet_aw_ave_as_is
Create Date: 2026-09-24
"""
from alembic import op

revision = "asg_matviews_owner"
down_revision = "sheet_aw_ave_as_is"
branch_labels = None
depends_on = None

_MATVIEWS = ("cirium.asg_commercial", "cirium.asg_business_helicopters", "cirium.asg_full",
             "cirium.non_asg_insured_commercial", "cirium.non_asg_insured_business")
_ROLE = "grp_aviation_write"


def _guarded(stmt: str) -> str:
    # a developer database restored without the cluster's group roles must still migrate
    return (f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_ROLE}') THEN "
            f"EXECUTE '{stmt}'; END IF; END $$;")


def upgrade() -> None:
    op.execute(_guarded(f"GRANT SELECT ON api.registration TO {_ROLE}"))
    for mv in _MATVIEWS:
        op.execute(_guarded(f"ALTER MATERIALIZED VIEW {mv} OWNER TO {_ROLE}"))


def downgrade() -> None:
    for mv in _MATVIEWS:
        op.execute(f"ALTER MATERIALIZED VIEW {mv} OWNER TO developer")
    op.execute(_guarded(f"REVOKE SELECT ON api.registration FROM {_ROLE}"))
