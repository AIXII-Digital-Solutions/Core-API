"""Index the audit log for the size it will be, and stop paying for four indexes that do nothing

Two different problems, one migration, because both are about indexes on the insured-fleet tables.

THE AUDIT LOG IS THE ONE TABLE HERE THAT GROWS WITHOUT BOUND. Everything else is a fleet: 149
aircraft, 1355 types, a policy a year. `audit.change_log` gets a row per write, for ever, and two
of its access paths were not indexed for that.

  * `GET /history/aircraft/{id}` has to reach the CHILD tables — engines, service, leases,
    coverage — and the log stores the child's id, so the aircraft is only named inside the JSONB
    snapshot. The query therefore filters on `new_row ->> 'aircraft_id'`, which nothing indexed:
    measured on today's small log it read 313 rows to return 3, and that ratio does not improve
    with age. Two PARTIAL expression indexes, one per snapshot, since only rows about a child of
    an aircraft carry the key at all.

  * The default listing is `ORDER BY changed_at DESC, id DESC`, and the index was on `changed_at`
    alone — enough to avoid a full sort, not enough to avoid re-sorting every group of equal
    timestamps, which the planner did as a visible Incremental Sort. Indexed in the order the
    query asks for, it is a plain backwards scan.

FOUR INDEXES THAT COST WRITES AND ANSWER NOTHING. Each is a bare foreign-key index whose column is
the LEADING column of a composite that already exists, so every lookup it could serve is already
served. They came from `index=True` on the FK, which is the right default and the wrong outcome
once a composite is added on top; the models now say `index=False` beside the reason.

    fleet.aircraft_engine   (aircraft_id)  -> uq_aircraft_engine_installation
    leasing.aircraft_lease  (aircraft_id)  -> uq_aircraft_lease_effective, ix_..._aircraft_effective
    policy.coverage         (aircraft_id)  -> uq_coverage_aircraft_policy
    policy.policy           (insured_id)   -> uq_policy_insured_period

Revision ID: audit_index_tuning
Revises: template_url_jsonb
Create Date: 2026-09-23
"""
from alembic import op

revision = "audit_index_tuning"
down_revision = "template_url_jsonb"
branch_labels = None
depends_on = None

_REDUNDANT = (
    ("fleet", "ix_fleet_aircraft_engine_aircraft_id"),
    ("leasing", "ix_leasing_aircraft_lease_aircraft_id"),
    ("policy", "ix_policy_coverage_aircraft_id"),
    ("policy", "ix_policy_policy_insured_id"),
)
_RECREATE = {
    "ix_fleet_aircraft_engine_aircraft_id": "fleet.aircraft_engine (aircraft_id)",
    "ix_leasing_aircraft_lease_aircraft_id": "leasing.aircraft_lease (aircraft_id)",
    "ix_policy_coverage_aircraft_id": "policy.coverage (aircraft_id)",
    "ix_policy_policy_insured_id": "policy.policy (insured_id)",
}


def upgrade() -> None:
    # --- the audit log ---------------------------------------------------------------------
    op.execute("DROP INDEX audit.ix_change_log_changed_at")
    op.execute("CREATE INDEX ix_change_log_changed_at ON audit.change_log "
               "(changed_at DESC, id DESC)")
    op.execute("CREATE INDEX ix_change_log_new_aircraft ON audit.change_log "
               "((new_row ->> 'aircraft_id')) "
               "WHERE new_row ->> 'aircraft_id' IS NOT NULL")
    op.execute("CREATE INDEX ix_change_log_old_aircraft ON audit.change_log "
               "((old_row ->> 'aircraft_id')) "
               "WHERE old_row ->> 'aircraft_id' IS NOT NULL")

    # --- the four that answer nothing ---------------------------------------------------------
    for schema, name in _REDUNDANT:
        op.execute(f"DROP INDEX {schema}.{name}")

    # Nothing here has been ANALYZEd since the category split doubled the type table, and two of
    # these tables have never been analyzed at all (reltuples = -1), so the planner is choosing
    # from guesses.
    for table in ("fleet.aircraft", "fleet.aircraft_type", "fleet.engine_type",
                  "fleet.aircraft_engine", "fleet.service_info", "ref.airline", "ref.party",
                  "ref.party_contact", "leasing.agreement", "leasing.aircraft_lease",
                  "policy.policy", "policy.coverage", "audit.change_log"):
        op.execute(f"ANALYZE {table}")


def downgrade() -> None:
    for name, target in _RECREATE.items():
        op.execute(f"CREATE INDEX {name} ON {target}")
    op.execute("DROP INDEX audit.ix_change_log_old_aircraft")
    op.execute("DROP INDEX audit.ix_change_log_new_aircraft")
    op.execute("DROP INDEX audit.ix_change_log_changed_at")
    op.execute("CREATE INDEX ix_change_log_changed_at ON audit.change_log (changed_at)")
