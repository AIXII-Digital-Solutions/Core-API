"""cirium: search indexes on ciriumaircrafts + all_/historical_/delta_ matviews

Revision ID: d1e2f3a4b5c6
Revises: c0d1e2f3a4b5
Create Date: 2026-07-01

Btree indexes for the common search fields (Operator, Manager, Owner, Registration, Status, period,
revision_number, is_historical) so filtered lookups on the base table, the plan_type matviews, and
the delta matviews don't seq-scan. The latest_* live VIEWs read cirium.ciriumaircrafts, so the base
table indexes speed those too; the asg_* matviews are tiny (airline-filtered) and keep their existing
indexes. Registration/Serial Number/revision_id are already indexed on ciriumaircrafts.
is_historical is constant on historical_* (always TRUE) so it is only indexed on all_*.
"""
from alembic import op

revision = "d1e2f3a4b5c6"
down_revision = "c0d1e2f3a4b5"
branch_labels = None
depends_on = None

# (object, [columns]) — index every column of every object below
TARGETS = []
TARGETS.append(("ciriumaircrafts", ["Operator", "Manager", "Owner", "Status"]))
_ALL_COLS = ["Registration", "Operator", "Manager", "Owner", "Status", "period", "revision_number", "is_historical"]
for mv in ("all_commercial", "all_business_helicopters"):
    TARGETS.append((mv, _ALL_COLS))
for mv in ("historical_commercial", "historical_business_helicopters"):
    TARGETS.append((mv, [c for c in _ALL_COLS if c != "is_historical"]))  # is_historical constant here
for mv in ("delta_commercial", "delta_business_helicopters", "delta_full"):
    TARGETS.append((mv, ["Operator", "Manager", "Owner", "Status"]))


def _name(obj: str, col: str) -> str:
    return f"ix_{obj}_{col.lower()}"


def upgrade() -> None:
    for obj, cols in TARGETS:
        for col in cols:
            op.execute(f'CREATE INDEX IF NOT EXISTS {_name(obj, col)} ON cirium.{obj} ("{col}")')


def downgrade() -> None:
    for obj, cols in TARGETS:
        for col in cols:
            op.execute(f"DROP INDEX IF EXISTS cirium.{_name(obj, col)}")
