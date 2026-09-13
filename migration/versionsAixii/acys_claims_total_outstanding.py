"""acys_claims: settled/outstanding stops being a row split and becomes two amount columns

A year's claims experience used to arrive as TWO rows — one `claims_status = 'Settled'`, one
'Ongoing' — so answering "what did this year cost" meant adding them back together, and every
consumer had to know that the split existed. It is now two columns of ONE row:

    claim_amount        -> claims_amount_total          everything the year cost
    (new)                  claims_amount_outstanding    the part still moving as a reserve
    claim_amounts_usd   -> claims_amount_total_usd
    (new)                  claims_amount_outstanding_usd
    claims_status       -> dropped, with its enum type

The settled figure is the difference of the two, so nothing is lost. The grain loses `claims_status`
as well: airline × calendar_year × currency × policy_type, still deliberately non-unique.

THE TABLE IS EMPTIED. The twelve rows it held were written under the old shape, where the amount on
a row meant "settled" or "outstanding" depending on a column that no longer exists — carrying them
over would mean deciding, per row, what the sheet had meant. The sheet is reloaded instead
(POST /forecast/claims/bulk), which is a minute's work and leaves no guesses in the data.

The two amounts are NOT constrained against each other: outstanding above total is arithmetic
nonsense, but a broker's sheet occasionally states it, and this table's contract is "as stated".

Revision ID: acys_claims_total_outstanding
Revises: forecast_run_snapshots
Create Date: 2026-09-13
"""
from alembic import op

revision = "acys_claims_total_outstanding"
down_revision = "forecast_run_snapshots"
branch_labels = None
depends_on = None

# asyncpg prepares every statement, so ONE op.execute() = ONE statement.
_UPGRADE = [
    # The rows describe a shape that is going away — see the docstring for why they are not mapped.
    "DELETE FROM forecast.acys_claims",

    "ALTER TABLE forecast.acys_claims RENAME COLUMN claim_amount TO claims_amount_total",
    "ALTER TABLE forecast.acys_claims RENAME COLUMN claim_amounts_usd TO claims_amount_total_usd",
    "ALTER TABLE forecast.acys_claims "
    "  ADD COLUMN claims_amount_outstanding numeric(18,2) NOT NULL DEFAULT 0",
    # The default exists only to fill the (empty) table during the ADD; the API always sends a value,
    # and leaving a default behind would let an omitted field pass silently as zero.
    "ALTER TABLE forecast.acys_claims ALTER COLUMN claims_amount_outstanding DROP DEFAULT",
    "ALTER TABLE forecast.acys_claims ADD COLUMN claims_amount_outstanding_usd numeric(18,2)",

    "ALTER TABLE forecast.acys_claims DROP COLUMN claims_status",
    "DROP TYPE IF EXISTS forecast.claims_status",

    # Constraints that named the renamed columns have to be rewritten, not just renamed: a CHECK
    # carries the column name inside its expression.
    "ALTER TABLE forecast.acys_claims DROP CONSTRAINT IF EXISTS ck_acys_claims_amount_non_negative",
    "ALTER TABLE forecast.acys_claims DROP CONSTRAINT IF EXISTS ck_acys_claims_usd_pair_complete",
    "ALTER TABLE forecast.acys_claims DROP CONSTRAINT IF EXISTS ck_acys_claims_usd_non_negative",
    "ALTER TABLE forecast.acys_claims "
    "  ADD CONSTRAINT ck_acys_claims_amount_non_negative CHECK (claims_amount_total >= 0)",
    "ALTER TABLE forecast.acys_claims "
    "  ADD CONSTRAINT ck_acys_claims_outstanding_non_negative "
    "  CHECK (claims_amount_outstanding >= 0)",
    "ALTER TABLE forecast.acys_claims "
    "  ADD CONSTRAINT ck_acys_claims_usd_pair_complete CHECK ("
    "    (currency_rate IS NULL) = (claims_amount_total_usd IS NULL)"
    "    AND (currency_rate IS NULL) = (claims_amount_outstanding_usd IS NULL))",
    "ALTER TABLE forecast.acys_claims "
    "  ADD CONSTRAINT ck_acys_claims_usd_non_negative "
    "  CHECK (claims_amount_total_usd IS NULL OR claims_amount_total_usd >= 0)",
    "ALTER TABLE forecast.acys_claims "
    "  ADD CONSTRAINT ck_acys_claims_usd_outstanding_non_negative "
    "  CHECK (claims_amount_outstanding_usd IS NULL OR claims_amount_outstanding_usd >= 0)",

    "DROP INDEX IF EXISTS forecast.ix_acys_claims_grain",
    "CREATE INDEX ix_acys_claims_grain ON forecast.acys_claims "
    "  (airline, calendar_year, currency, policy_type)",
]

_DOWNGRADE = [
    "DELETE FROM forecast.acys_claims",
    "DROP INDEX IF EXISTS forecast.ix_acys_claims_grain",
    "ALTER TABLE forecast.acys_claims DROP CONSTRAINT IF EXISTS ck_acys_claims_amount_non_negative",
    "ALTER TABLE forecast.acys_claims "
    "  DROP CONSTRAINT IF EXISTS ck_acys_claims_outstanding_non_negative",
    "ALTER TABLE forecast.acys_claims DROP CONSTRAINT IF EXISTS ck_acys_claims_usd_pair_complete",
    "ALTER TABLE forecast.acys_claims DROP CONSTRAINT IF EXISTS ck_acys_claims_usd_non_negative",
    "ALTER TABLE forecast.acys_claims "
    "  DROP CONSTRAINT IF EXISTS ck_acys_claims_usd_outstanding_non_negative",
    "ALTER TABLE forecast.acys_claims DROP COLUMN claims_amount_outstanding",
    "ALTER TABLE forecast.acys_claims DROP COLUMN claims_amount_outstanding_usd",
    "ALTER TABLE forecast.acys_claims RENAME COLUMN claims_amount_total TO claim_amount",
    "ALTER TABLE forecast.acys_claims RENAME COLUMN claims_amount_total_usd TO claim_amounts_usd",
    "CREATE TYPE forecast.claims_status AS ENUM ('Settled', 'Ongoing')",
    "ALTER TABLE forecast.acys_claims "
    "  ADD COLUMN claims_status forecast.claims_status NOT NULL DEFAULT 'Settled'",
    "ALTER TABLE forecast.acys_claims ALTER COLUMN claims_status DROP DEFAULT",
    "ALTER TABLE forecast.acys_claims "
    "  ADD CONSTRAINT ck_acys_claims_amount_non_negative CHECK (claim_amount >= 0)",
    "ALTER TABLE forecast.acys_claims "
    "  ADD CONSTRAINT ck_acys_claims_usd_pair_complete "
    "  CHECK ((currency_rate IS NULL) = (claim_amounts_usd IS NULL))",
    "ALTER TABLE forecast.acys_claims "
    "  ADD CONSTRAINT ck_acys_claims_usd_non_negative "
    "  CHECK (claim_amounts_usd IS NULL OR claim_amounts_usd >= 0)",
    "CREATE INDEX ix_acys_claims_grain ON forecast.acys_claims "
    "  (airline, calendar_year, currency, policy_type, claims_status)",
]


def upgrade() -> None:
    for stmt in _UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in _DOWNGRADE:
        op.execute(stmt)
