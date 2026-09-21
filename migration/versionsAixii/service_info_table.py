"""The service block becomes a table of its own: fleet.service_info

The six fields the specification groups as "service information" were scattered across three
tables — `agreed_value_fixed`, `source`, `status` and `usage_status` on `leasing.aircraft_lease`,
the lease currency on `leasing.agreement`, the policy currency on `policy.policy`. They are one
block about one aircraft, so they become one row per aircraft in `fleet`, beside the airframe they
describe.

    fleet.service_info    aircraft_id (1:1)
                          agreed_value_fixed, source, status, usage_status,
                          lease_currency, policy_currency

`source` now defaults to **'cirium'**: most records arrive from the Cirium feed, and a default that
matches the common case is one less field to fill in per aircraft. The other two enum-backed
columns keep their meaning — `status` defaults to `insured`, `usage_status` stays free text because
Cirium owns that vocabulary and adds to it.

The two enums move with their columns: `leasing.lease_source` and `leasing.insurance_status` are
recreated as `fleet.record_source` and `fleet.insurance_status`. `lease_source` is renamed because
the column is no longer about a lease.

WHAT THIS GIVES UP, and it is worth knowing. The currencies were properties of a CONTRACT: one
lease agreement covers several aircraft in one currency, one policy likewise. Held per aircraft,
nothing stops two aircraft on the same agreement from recording different currencies for it. The
schema can no longer state that they must agree; whoever writes them has to. If that turns out to
matter, `currency` goes back on `leasing.agreement` / `policy.policy` and the service row keeps the
rest — the two are not entangled.

Nothing is migrated: all four affected tables were empty.

Revision ID: service_info_table
Revises: lease_status_columns
Create Date: 2026-09-21
"""
from alembic import op

revision = "service_info_table"
down_revision = "lease_status_columns"
branch_labels = None
depends_on = None

_READ_ROLES = "grp_aixii_read, grp_aviation_write, svc_external_worker"
_WRITE_ROLE = "grp_api_write"
_CURRENCIES = "'USD', 'EUR', 'GBP'"


def upgrade() -> None:
    # --- out of the three tables they were scattered across ------------------------------------
    for statement in (
        "ALTER TABLE leasing.aircraft_lease DROP COLUMN IF EXISTS agreed_value_fixed",
        "ALTER TABLE leasing.aircraft_lease DROP COLUMN IF EXISTS source",
        "ALTER TABLE leasing.aircraft_lease DROP COLUMN IF EXISTS status",
        "ALTER TABLE leasing.aircraft_lease DROP COLUMN IF EXISTS usage_status",
        "ALTER TABLE leasing.agreement DROP COLUMN IF EXISTS currency",
        "ALTER TABLE policy.policy DROP COLUMN IF EXISTS currency",
        "DROP TYPE IF EXISTS leasing.lease_source",
        "DROP TYPE IF EXISTS leasing.insurance_status",
    ):
        op.execute(statement)

    # --- and into one row per aircraft ----------------------------------------------------------
    op.execute("CREATE TYPE fleet.record_source AS ENUM ('manual', 'lease_agreement', 'cirium')")
    op.execute("CREATE TYPE fleet.insurance_status AS ENUM ('insured', 'not_insured')")
    op.execute(f"""
        CREATE TABLE fleet.service_info (
            id                 bigserial PRIMARY KEY,
            aircraft_id        bigint NOT NULL
                               REFERENCES fleet.aircraft (id) ON DELETE CASCADE,
            agreed_value_fixed boolean NOT NULL DEFAULT false,
            source             fleet.record_source NOT NULL DEFAULT 'cirium',
            status             fleet.insurance_status NOT NULL DEFAULT 'insured',
            usage_status       text,
            lease_currency     varchar(3) NOT NULL DEFAULT 'USD',
            policy_currency    varchar(3) NOT NULL DEFAULT 'USD',
            created_at         timestamp NOT NULL DEFAULT now(),
            updated_at         timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_service_info_aircraft UNIQUE (aircraft_id),
            CONSTRAINT ck_service_info_lease_currency
                CHECK (lease_currency IN ({_CURRENCIES})
                       AND lease_currency = upper(lease_currency)),
            CONSTRAINT ck_service_info_policy_currency
                CHECK (policy_currency IN ({_CURRENCIES})
                       AND policy_currency = upper(policy_currency))
        )
    """)
    op.execute("CREATE INDEX ix_service_info_status ON fleet.service_info (status)")
    op.execute("CREATE INDEX ix_service_info_source ON fleet.service_info (source)")

    op.execute(
        "COMMENT ON TABLE fleet.service_info IS "
        "'The specification''s service block, one row per aircraft: how the record came to be "
        "(source), whether it is covered (status), what Cirium says the airframe is doing "
        "(usage_status), whether the agreed value depreciates, and the two contract currencies. "
        "Bookkeeping metadata, NOT maintenance.'"
    )
    op.execute(
        "COMMENT ON COLUMN fleet.service_info.source IS "
        "'Where this aircraft''s record came from. Defaults to cirium - most arrive from the feed.'"
    )
    op.execute(
        "COMMENT ON COLUMN fleet.service_info.status IS "
        "'Whether the aircraft is covered. not_insured states a KNOWN gap, which is different from "
        "an aircraft nobody has entered a policy for - /policy/coverage/compare reads it so a "
        "deliberate gap is not reported as a mistake.'"
    )
    op.execute(
        "COMMENT ON COLUMN fleet.service_info.usage_status IS "
        "'The airframe''s operational status as Cirium states it (In Service, Storage, Retired, "
        "Written off, Type swap, ...). Text, not an enum: Cirium owns the vocabulary and adds to it.'"
    )
    op.execute(
        "COMMENT ON COLUMN fleet.service_info.lease_currency IS "
        "'The lease agreement''s currency. Held per aircraft since revision service_info_table, so "
        "nothing stops two aircraft on one agreement disagreeing - whoever writes them must keep "
        "them consistent.'"
    )

    op.execute(
        "CREATE TRIGGER service_info_audit AFTER INSERT OR UPDATE OR DELETE ON fleet.service_info "
        "FOR EACH ROW EXECUTE FUNCTION audit.log_change()"
    )
    op.execute(f"GRANT SELECT ON fleet.service_info TO {_READ_ROLES}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON fleet.service_info TO {_WRITE_ROLE}")
    op.execute(f"GRANT USAGE, SELECT ON SEQUENCE fleet.service_info_id_seq TO {_WRITE_ROLE}")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS service_info_audit ON fleet.service_info")
    op.execute("DROP TABLE IF EXISTS fleet.service_info")
    op.execute("DROP TYPE IF EXISTS fleet.record_source")
    op.execute("DROP TYPE IF EXISTS fleet.insurance_status")

    op.execute("CREATE TYPE leasing.lease_source AS ENUM ('manual', 'lease_agreement', 'cirium')")
    op.execute("CREATE TYPE leasing.insurance_status AS ENUM ('insured', 'not_insured')")
    op.execute(
        "ALTER TABLE leasing.aircraft_lease ADD COLUMN agreed_value_fixed boolean "
        "NOT NULL DEFAULT false"
    )
    op.execute(
        "ALTER TABLE leasing.aircraft_lease ADD COLUMN source leasing.lease_source "
        "NOT NULL DEFAULT 'manual'"
    )
    op.execute(
        "ALTER TABLE leasing.aircraft_lease ADD COLUMN status leasing.insurance_status "
        "NOT NULL DEFAULT 'insured'"
    )
    op.execute("ALTER TABLE leasing.aircraft_lease ADD COLUMN usage_status text")
    op.execute("CREATE INDEX ix_aircraft_lease_status ON leasing.aircraft_lease (status)")
    op.execute(
        "ALTER TABLE leasing.agreement ADD COLUMN currency varchar(3) NOT NULL DEFAULT 'USD'"
    )
    op.execute(f"ALTER TABLE leasing.agreement ADD CONSTRAINT ck_agreement_currency "
               f"CHECK (currency IN ({_CURRENCIES}))")
    op.execute("ALTER TABLE leasing.agreement ADD CONSTRAINT ck_agreement_currency_upper "
               "CHECK (currency = upper(currency))")
    op.execute("ALTER TABLE policy.policy ADD COLUMN currency varchar(3) NOT NULL DEFAULT 'USD'")
    op.execute(f"ALTER TABLE policy.policy ADD CONSTRAINT ck_policy_currency "
               f"CHECK (currency IN ({_CURRENCIES}))")
    op.execute("ALTER TABLE policy.policy ADD CONSTRAINT ck_policy_currency_upper "
               "CHECK (currency = upper(currency))")
