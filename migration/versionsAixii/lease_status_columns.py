"""The service block gains an insurance status and the aircraft's usage status

Two columns on `leasing.aircraft_lease`, beside `source` and `agreed_value_fixed` — the block that
says HOW a record came to be rather than what it agrees.

  status        insured | not_insured, default insured. A `not_insured` record is a statement that
                the aircraft is KNOWINGLY uncovered over this period, which is different from an
                aircraft nobody has entered a policy for yet. `/policy/coverage/compare` reads it,
                so a deliberate gap stops being reported as a missing policy.

  usage_status  the aircraft's operational status as Cirium states it — 'In Service', 'Storage',
                'Retired', 'Written off', 'On order', 'Type swap', 'Reengineered', 'Cancelled' and
                the rest.

WHY usage_status IS TEXT AND NOT AN ENUM. Cirium currently uses 12 values and the set grows on
Cirium's schedule, not ours: 'LOI to Order', 'LOI to Option', 'Type swap' and 'Reengineered' are
not a vocabulary anyone would have predicted. An enum would turn each new value into a migration
that blocks an import. Store what Cirium said, verbatim.

A NOTE ON WHERE usage_status LIVES. It describes the AIRFRAME, not the contract, so writing it here
makes it a snapshot as of this record rather than a live value: keeping it current would mean
PATCHing a lease record, and that is an audited contract change. If it should track Cirium
continuously, it belongs on `fleet.aircraft` and a sync job should own it — one column move.

Revision ID: lease_status_columns
Revises: engine_type_table
Create Date: 2026-09-21
"""
from alembic import op

revision = "lease_status_columns"
down_revision = "engine_type_table"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE TYPE leasing.insurance_status AS ENUM ('insured', 'not_insured')")
    op.execute(
        "ALTER TABLE leasing.aircraft_lease ADD COLUMN status leasing.insurance_status "
        "NOT NULL DEFAULT 'insured'"
    )
    op.execute("ALTER TABLE leasing.aircraft_lease ADD COLUMN usage_status text")
    op.execute("CREATE INDEX ix_aircraft_lease_status ON leasing.aircraft_lease (status)")

    op.execute(
        "COMMENT ON COLUMN leasing.aircraft_lease.status IS "
        "'Whether the aircraft is covered over this record''s period. not_insured states a KNOWN "
        "gap, which is different from an aircraft nobody has entered a policy for - "
        "/policy/coverage/compare reads it so a deliberate gap is not reported as a mistake.'"
    )
    op.execute(
        "COMMENT ON COLUMN leasing.aircraft_lease.usage_status IS "
        "'The aircraft''s operational status as Cirium states it (In Service, Storage, Retired, "
        "Written off, Type swap, ...). Text, not an enum: Cirium owns the vocabulary and adds to "
        "it. A snapshot as of this record - if it must track Cirium continuously it belongs on "
        "fleet.aircraft with a sync job.'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS leasing.ix_aircraft_lease_status")
    op.execute("ALTER TABLE leasing.aircraft_lease DROP COLUMN IF EXISTS usage_status")
    op.execute("ALTER TABLE leasing.aircraft_lease DROP COLUMN IF EXISTS status")
    op.execute("DROP TYPE IF EXISTS leasing.insurance_status")
