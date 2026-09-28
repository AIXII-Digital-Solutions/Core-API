"""Drop fleet.service_info.policy_currency, replaced by policy.policy.currency

Second half of policy_currency_on_policy. Apply it only once the code that reads
policy.policy.currency is deployed: the code before it still selects the old column.

Revision ID: policy_currency_drop_old
Revises: policy_currency_on_policy
Create Date: 2026-09-29
"""
from alembic import op

revision = "policy_currency_drop_old"
down_revision = "policy_currency_on_policy"
branch_labels = None
depends_on = None

_CURRENCIES = "'USD', 'EUR', 'GBP'"


def upgrade() -> None:
    op.execute("ALTER TABLE fleet.service_info DROP CONSTRAINT IF EXISTS ck_service_info_policy_currency")
    op.execute("ALTER TABLE fleet.service_info DROP COLUMN policy_currency")


def downgrade() -> None:
    op.execute("ALTER TABLE fleet.service_info ADD COLUMN policy_currency varchar(3) NOT NULL DEFAULT 'USD'")
    op.execute(f"ALTER TABLE fleet.service_info ADD CONSTRAINT ck_service_info_policy_currency "
               f"CHECK (policy_currency IN ({_CURRENCIES}) AND policy_currency = upper(policy_currency))")
    # each aircraft takes the currency of the policy covering it today, else of its latest one
    op.execute("""
        UPDATE fleet.service_info s SET policy_currency = x.currency
        FROM (SELECT DISTINCT ON (c.aircraft_id) c.aircraft_id, p.currency
              FROM policy.coverage c JOIN policy.policy p ON p.id = c.policy_id
              ORDER BY c.aircraft_id,
                       (c.covered_from <= current_date
                        AND (c.covered_to IS NULL OR c.covered_to >= current_date)) DESC,
                       c.covered_from DESC) x
        WHERE x.aircraft_id = s.aircraft_id
    """)
