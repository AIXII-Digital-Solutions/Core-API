"""Hand the cirium plan-type matviews to grp_aviation_write so external-worker can REFRESH them.

external-worker's crons (RegsListUpdater: asg_regs_updater / refresh_cirium_delta / refresh_plantype_matviews
/ collapse_completed_revisions) REFRESH these ten matviews on schedule. In production the worker connects as
``svc_external_worker``, and REFRESH MATERIALIZED VIEW requires OWNERSHIP (or membership in the owning role) —
but these ten are owned by ``developer``, which svc_external_worker is NOT a member of, so every refresh would
fail with "must be owner of materialized view". They are handed to ``grp_aviation_write`` (svc_external_worker
IS a member) — the same role that already owns cirium.airlines / cirium.registrations and the forecast /
airport-geo matviews, exactly so the worker can refresh them.

A REFRESH runs the matview body as its OWNER, so the new owner grp_aviation_write must be able to read every
source. It already holds SELECT on cirium.ciriumaircrafts / cirium.aircraftrevision (and the asg_*/delta_*
matviews the _full/all_* ones read), but NOT on api.airlines — which the asg_* matviews join for the airline
match — so this migration also GRANTs that. (api.airlines is owned by developer, the migration user, so the
grant is permitted.) The migration user developer both owns the ten matviews and is a member of
grp_aviation_write, so it may reassign ownership.

(Companion to the external-worker fix where Database/Client.refresh_materialized_view was awaiting
AsyncConnection.execution_options — a SQLAlchemy-2.0 coroutine — before this ownership even mattered.)

Revision ID: cirium_matviews_owner
Revises: bucket_matviews_from_grouped
Create Date: 2026-07-20
"""
from alembic import op

revision = "cirium_matviews_owner"
down_revision = "bucket_matviews_from_grouped"
branch_labels = None
depends_on = None

_MATVIEWS = [
    "cirium.asg_commercial", "cirium.asg_business_helicopters", "cirium.asg_full",
    "cirium.delta_commercial", "cirium.delta_business_helicopters", "cirium.delta_full",
    "cirium.all_commercial", "cirium.all_business_helicopters",
    "cirium.historical_commercial", "cirium.historical_business_helicopters",
]


def _reassign(owner: str) -> None:
    stmts = "\n".join(
        f"    EXECUTE 'ALTER MATERIALIZED VIEW {mv} OWNER TO {owner}';" for mv in _MATVIEWS)
    op.execute(f"""
    DO $$
    BEGIN
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{owner}') THEN
{stmts}
      END IF;
    END $$;
    """)


_GRANT_SOURCES = """
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aviation_write') THEN
    -- the asg_* matviews join api.airlines for the airline match; the new owner needs to read it to REFRESH
    EXECUTE 'GRANT USAGE ON SCHEMA api TO grp_aviation_write';
    EXECUTE 'GRANT SELECT ON api.airlines TO grp_aviation_write';
  END IF;
END $$;
"""


def upgrade() -> None:
    op.execute(_GRANT_SOURCES)   # BEFORE the handover, so the new owner can refresh immediately
    _reassign("grp_aviation_write")


def downgrade() -> None:
    # Revert ownership only; the api.airlines grant is left in place — it is harmless, and revoking it is not
    # this migration's business (nothing else depends on its absence).
    _reassign("developer")
