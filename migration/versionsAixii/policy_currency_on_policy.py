"""The policy currency belongs to the policy, not to each aircraft on it

Revision service_info_table moved `policy.policy.currency` into the per-aircraft service block as
`fleet.service_info.policy_currency`. A currency is a term of the CONTRACT — every amount on the
policy is in it — and held per aircraft, two airframes on one policy could disagree with nothing to
stop them. It comes back:

  * policy.policy.currency, varchar(3) NOT NULL DEFAULT 'USD', USD / EUR / GBP, upper-case;
  * each existing policy takes the currency its covered aircraft carried — the most common one, and
    the migration stops if they disagree rather than pick one silently; a policy with no aircraft
    stays USD;
  * fleet.service_info.policy_currency is LEFT IN PLACE here and dropped by the next revision,
    policy_currency_drop_old. main deploys itself, so a drop in the same step would break
    whichever code is running in between: the old code reads the old column, the new code the new
    one. Apply this, let the new code deploy, then apply the drop. (lease_currency stays.)

Revision ID: policy_currency_on_policy
Revises: country_names_plain
Create Date: 2026-09-29
"""
from alembic import op

revision = "policy_currency_on_policy"
down_revision = "country_names_plain"
branch_labels = None
depends_on = None

_CURRENCIES = "'USD', 'EUR', 'GBP'"


def upgrade() -> None:
    op.execute("ALTER TABLE policy.policy ADD COLUMN currency varchar(3) NOT NULL DEFAULT 'USD'")
    op.execute(f"ALTER TABLE policy.policy ADD CONSTRAINT ck_policy_currency "
               f"CHECK (currency IN ({_CURRENCIES}) AND currency = upper(currency))")
    op.execute("COMMENT ON COLUMN policy.policy.currency IS "
               "'The currency every amount on this policy is in.'")
    op.execute("""
        DO $do$
        DECLARE r record;
        BEGIN
            FOR r IN SELECT c.policy_id, array_agg(DISTINCT s.policy_currency) AS found
                     FROM policy.coverage c JOIN fleet.service_info s ON s.aircraft_id = c.aircraft_id
                     GROUP BY c.policy_id HAVING count(DISTINCT s.policy_currency) > 1
            LOOP
                RAISE EXCEPTION 'policy % covers aircraft in several policy currencies: %',
                    r.policy_id, r.found;
            END LOOP;
        END
        $do$
    """)
    op.execute("SELECT set_config('app.actor', 'migration policy_currency_on_policy', true)")
    op.execute("""
        UPDATE policy.policy p SET currency = x.currency
        FROM (SELECT c.policy_id, min(s.policy_currency) AS currency
              FROM policy.coverage c JOIN fleet.service_info s ON s.aircraft_id = c.aircraft_id
              GROUP BY c.policy_id) x
        WHERE x.policy_id = p.id AND p.currency IS DISTINCT FROM x.currency
    """)
    op.execute("COMMENT ON COLUMN fleet.service_info.policy_currency IS 'DEPRECATED - the policy "
               "currency is policy.policy.currency. Unused by the API; dropped by revision "
               "policy_currency_drop_old.'")


def downgrade() -> None:
    op.execute("ALTER TABLE policy.policy DROP CONSTRAINT IF EXISTS ck_policy_currency")
    op.execute("ALTER TABLE policy.policy DROP COLUMN currency")
