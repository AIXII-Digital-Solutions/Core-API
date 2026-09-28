"""Several parties per policy role, the share a cession is taken of, and each airframe's MTOW

1. policy.policy_party. A policy's insured, reinsured and retrocedent were one column each, but a
   schedule routinely names several of each (co-insured group companies, a panel of reinsurers).
   They become rows: (policy, party, role, position), `position` being the order the schedule
   lists them in. Existing links are carried over as position 1, then the three columns go — and
   with them uq_policy_insured_period, the natural key (insured, period), which a SET of insured
   can no longer express. The table is audited like every other in the domain.

2. policy.policy.reinsured_amount_of — the share, in percent, that `reinsured_amount` is a
   percentage of: 97.5 of 100.

3. fleet.aircraft.mtow_kg — the airframe's maximum take-off weight in kg. Back-filled here from
   the newest Cirium revision: Operating MTOW (the weight this airframe is registered at) or, when
   Cirium has none, Certified MTOW; converted from lbs. Editable afterwards; nothing re-syncs it.

Revision ID: policy_parties_mtow
Revises: service_status_sync
Create Date: 2026-09-28
"""
from alembic import op

revision = "policy_parties_mtow"
down_revision = "service_status_sync"
branch_labels = None
depends_on = None

_READ_ROLES = "grp_aixii_read, grp_aviation_write, svc_external_worker"
_WRITE_ROLE = "grp_api_write"
_ROLES = (("insured", "insured_id"), ("reinsured", "reinsured_id"),
          ("retrocedent", "retrocedent_id"))

_MTOW_BACKFILL = r"""
UPDATE fleet.aircraft a
SET mtow_kg = w.kg
FROM (
    SELECT DISTINCT ON (t.id) t.id,
           round(coalesce(c."Operating MTOW (lbs)", c."Certified MTOW (lbs)") * 0.45359237)::int AS kg
    FROM fleet.aircraft t
    JOIN cirium.ciriumaircrafts c
      ON upper(regexp_replace(c."Registration", '[^A-Za-z0-9]', '', 'g')) = t.registration_normalized
     AND c.revision_id IN (SELECT max(r.id) FROM cirium.aircraftrevision r
                           WHERE r.plan_type IN ('Commercial', 'Business&Helicopters')
                           GROUP BY r.plan_type)
    WHERE coalesce(c."Operating MTOW (lbs)", c."Certified MTOW (lbs)") > 0
    ORDER BY t.id,
             (btrim(c."Serial Number") = t.msn) DESC NULLS LAST,
             (c."Status" IN ('Cancelled', 'On order', 'Retired', 'Written off')) NULLS LAST,
             c.revision_id DESC, c.id DESC
) w
WHERE a.id = w.id AND a.mtow_kg IS NULL
"""


def upgrade() -> None:
    # --- 1. parties ---------------------------------------------------------------------------
    op.execute("CREATE TYPE policy.party_role AS ENUM ('insured', 'reinsured', 'retrocedent')")
    op.execute("""
        CREATE TABLE policy.policy_party (
            id          bigserial PRIMARY KEY,
            policy_id   bigint NOT NULL REFERENCES policy.policy (id) ON DELETE CASCADE,
            party_id    bigint NOT NULL REFERENCES ref.party (id) ON DELETE RESTRICT,
            role        policy.party_role NOT NULL,
            position    smallint NOT NULL DEFAULT 1,
            created_at  timestamp NOT NULL DEFAULT now(),
            updated_at  timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_policy_party UNIQUE (policy_id, role, party_id),
            CONSTRAINT ck_policy_party_position CHECK (position >= 1)
        )
    """)
    op.execute("CREATE INDEX ix_policy_policy_party_party_id ON policy.policy_party (party_id)")
    op.execute("COMMENT ON TABLE policy.policy_party IS 'Who is party to a policy and as what: "
               "any number of insured, reinsured and retrocedent entities, position = the order "
               "the schedule lists them in.'")
    for role, column in _ROLES:
        op.execute(f"INSERT INTO policy.policy_party (policy_id, party_id, role) "
                   f"SELECT id, {column}, '{role}' FROM policy.policy WHERE {column} IS NOT NULL")
    op.execute("ALTER TABLE policy.policy DROP CONSTRAINT IF EXISTS uq_policy_insured_period")
    for _, column in _ROLES:
        op.execute(f"ALTER TABLE policy.policy DROP COLUMN {column}")
    op.execute("CREATE TRIGGER policy_party_audit AFTER INSERT OR UPDATE OR DELETE "
               "ON policy.policy_party FOR EACH ROW EXECUTE FUNCTION audit.log_change()")
    op.execute(f"GRANT SELECT ON policy.policy_party TO {_READ_ROLES}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON policy.policy_party TO {_WRITE_ROLE}")
    op.execute(f"GRANT USAGE, SELECT ON SEQUENCE policy.policy_party_id_seq TO {_WRITE_ROLE}")

    # --- 2. the share a cession is taken of -----------------------------------------------------
    op.execute("ALTER TABLE policy.policy ADD COLUMN reinsured_amount_of numeric(6, 3)")
    op.execute("ALTER TABLE policy.policy ADD CONSTRAINT ck_policy_reinsured_amount_of "
               "CHECK (reinsured_amount_of IS NULL OR reinsured_amount_of BETWEEN 0 AND 100)")
    op.execute("COMMENT ON COLUMN policy.policy.reinsured_amount_of IS 'The share, in percent, "
               "that reinsured_amount is a percentage of (97.5 of 100).'")

    # --- 3. MTOW ----------------------------------------------------------------------------------
    op.execute("ALTER TABLE fleet.aircraft ADD COLUMN mtow_kg integer")
    op.execute("ALTER TABLE fleet.aircraft ADD CONSTRAINT ck_aircraft_mtow_kg "
               "CHECK (mtow_kg IS NULL OR mtow_kg > 0)")
    op.execute("COMMENT ON COLUMN fleet.aircraft.mtow_kg IS 'Maximum take-off weight, kg, of THIS "
               "airframe. Pre-filled from Cirium''s Operating MTOW (else Certified), converted from "
               "lbs; editable.'")
    op.execute("SELECT set_config('app.actor', 'migration policy_parties_mtow', true)")
    op.execute(_MTOW_BACKFILL)


def downgrade() -> None:
    op.execute("ALTER TABLE fleet.aircraft DROP CONSTRAINT IF EXISTS ck_aircraft_mtow_kg")
    op.execute("ALTER TABLE fleet.aircraft DROP COLUMN IF EXISTS mtow_kg")
    op.execute("ALTER TABLE policy.policy DROP CONSTRAINT IF EXISTS ck_policy_reinsured_amount_of")
    op.execute("ALTER TABLE policy.policy DROP COLUMN IF EXISTS reinsured_amount_of")

    # Back to one party per role: the FIRST of each (lowest position). Any others are lost, and a
    # policy with no insured cannot be restored at all — the downgrade refuses rather than guess.
    op.execute("""
        DO $do$
        BEGIN
            IF EXISTS (SELECT 1 FROM policy.policy p WHERE NOT EXISTS (
                           SELECT 1 FROM policy.policy_party x
                           WHERE x.policy_id = p.id AND x.role = 'insured')) THEN
                RAISE EXCEPTION 'a policy has no insured; cannot restore insured_id NOT NULL';
            END IF;
        END
        $do$
    """)
    for _, column in _ROLES:
        op.execute(f"ALTER TABLE policy.policy ADD COLUMN {column} bigint "
                   f"REFERENCES ref.party (id) ON DELETE RESTRICT")
    for role, column in _ROLES:
        op.execute(f"""
            UPDATE policy.policy p SET {column} = x.party_id
            FROM (SELECT DISTINCT ON (policy_id) policy_id, party_id FROM policy.policy_party
                  WHERE role = '{role}' ORDER BY policy_id, position, id) x
            WHERE x.policy_id = p.id
        """)
    op.execute("ALTER TABLE policy.policy ALTER COLUMN insured_id SET NOT NULL")
    op.execute("CREATE INDEX ix_policy_policy_reinsured_id ON policy.policy (reinsured_id)")
    op.execute("CREATE INDEX ix_policy_policy_retrocedent_id ON policy.policy (retrocedent_id)")
    op.execute("ALTER TABLE policy.policy ADD CONSTRAINT uq_policy_insured_period "
               "UNIQUE NULLS NOT DISTINCT (insured_id, period_from, period_to)")
    op.execute("DROP TABLE policy.policy_party")
    op.execute("DROP TYPE policy.party_role")
