"""powerbi.last_seen_fleet is readable by the BI role, and stays readable when it is rebuilt

`bi_reader` could read every view and matview the platform has — 44 of them — except the one the
fleet report actually runs on. `powerbi.last_seen_fleet` carried no ACL at all, owner only.

WHY IT KEPT LOSING THE GRANT. A grant lives on the object, and the view has been dropped and
recreated five times (`pbi_last_seen_fleet` -> `pbi_fleet_view_v2` -> `reg_airline_fallback` ->
`pbi_fleet_seen_only` -> `pbi_ground_snap`), each time coming back bare. Its neighbours in the same
schema — `z_dates`, `z_top_n`, `group_by_orig_dest` — kept theirs only because nothing has dropped
them since somebody granted them by hand.

THE ACTUAL FAULT is that `powerbi` is the ONE schema in this database with no entry in
`pg_default_acl`. cirium, flightradar, forecast, api, ref, fleet, leasing, policy, audit, airlabs
and aviationedge all have default privileges, so an object rebuilt there is readable again the
moment it exists. So this grants what is missing today AND sets the default, which is what stops
the next rebuild of the report from silently locking BI out again.

Granted to the GROUP, not to the user: `bi_reader` is a member of `grp_aixii_read`, and that is how
every other read grant in this database is written (see docs/db-grant-access.md). Adding a user to
the group is then the whole of "give this person BI access".

A view runs with its OWNER's rights over the tables it reads, so `bi_reader` needs nothing on
cirium, flightradar or ref for this to work — only SELECT on the view itself.

Revision ID: powerbi_read_grants
Revises: align_type_tables
Create Date: 2026-09-21
"""
from alembic import op

revision = "powerbi_read_grants"
down_revision = "align_type_tables"
branch_labels = None
depends_on = None

# what the schema's other objects already grant
_READ_ROLES = "grp_aixii_read, grp_aviation_write"


def upgrade() -> None:
    op.execute(f"GRANT USAGE ON SCHEMA powerbi TO {_READ_ROLES}")
    op.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA powerbi TO {_READ_ROLES}")
    # the half that was missing: without this, the next DROP+CREATE of a report view comes back
    # unreadable again and nobody notices until a dashboard is empty
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA powerbi GRANT SELECT ON TABLES TO {_READ_ROLES}")
    op.execute(
        "COMMENT ON SCHEMA powerbi IS "
        "'Report objects for PowerBI. Read by bi_reader through grp_aixii_read. Default privileges "
        "are set on this schema, so a view rebuilt by a migration is readable again as soon as it "
        "exists - it was not, and last_seen_fleet lost its grant on every rebuild.'"
    )


def downgrade() -> None:
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA powerbi REVOKE SELECT ON TABLES FROM {_READ_ROLES}")
    op.execute("REVOKE SELECT ON powerbi.last_seen_fleet FROM grp_aixii_read, grp_aviation_write")
    op.execute("COMMENT ON SCHEMA powerbi IS NULL")
