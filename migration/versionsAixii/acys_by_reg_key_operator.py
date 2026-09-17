"""The by-reg refresh key was missing the Operator, and a wet-leased tail has two of them

`uq_acys_by_reg_refresh` was (MERGED_KEY, Data Type, Contract Year). MERGED_KEY is
Registration|Aircraft Sub Series|Period — no operator in it — and that is not the matview's grain:
`acys_summary_grouped_by_reg` groups by Operator as well, because ONE tail can fly for TWO operators
in the same month. A wet lease produces exactly that: the aircraft appears under the operator that
flies it (Sub Lease / Wet, sentinel Agreed Value) and under the one that holds it (Lease / Dry, the
real Agreed Value). Both rows are correct and both are wanted in the report.

So the index was not a key at all, and the forecast run died on it — a plain REFRESH rebuilds the
index and the rebuild failed:

    could not create unique index "uq_acys_by_reg_refresh"
    DETAIL: Key ("MERGED_KEY", "Data Type", "Contract Year")=(TC-GPD|A321-231|09-2026, Forecast,
    CY2026) is duplicated.

Measured on production before writing this: 251 groups violate the old key; adding "Operator" makes
it unique over the whole matview (0 duplicate groups). "Date" is added as well — it is functionally
dependent on the rest today, but the anchor month stamps two dates and only the Contract-Year split
keeps them apart, so a clamp that puts both on the same side of the boundary would break the key
again. A refresh key costs nothing to widen.

DOWNGRADE WILL FAIL on real data, on purpose: re-creating the narrow index is re-creating the bug,
and it can only succeed on a dataset with no wet leases.

Revision ID: acys_by_reg_key_operator
Revises: acys_drop_unused_report_idx
Create Date: 2026-09-17
"""
from alembic import op

revision = "acys_by_reg_key_operator"
down_revision = "acys_drop_unused_report_idx"
branch_labels = None
depends_on = None

_MATVIEW = "forecast.acys_summary_grouped_by_reg"
_NAME = "uq_acys_by_reg_refresh"
_NEW = '"MERGED_KEY", "Data Type", "Contract Year", "Operator", "Date"'
_OLD = '"MERGED_KEY", "Data Type", "Contract Year"'


def upgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS forecast.{_NAME}")
    op.execute(f"CREATE UNIQUE INDEX {_NAME} ON {_MATVIEW} ({_NEW})")


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS forecast.{_NAME}")
    op.execute(f"CREATE UNIQUE INDEX {_NAME} ON {_MATVIEW} ({_OLD})")
