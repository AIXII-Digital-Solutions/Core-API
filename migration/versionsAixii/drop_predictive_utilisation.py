"""Remove the predictive_utilisation feature entirely: drop api.predictive_utilisation and its two
SECURITY DEFINER functions. The endpoint (Core-API), the worker pipeline/cleanup, the ARQ tasks and
the schedule entry are removed in code; this migration removes the DB objects.

Revision ID: drop_predictive_util
Revises: flightsummary_coverage
Create Date: 2026-07-08
"""
from alembic import op

revision = "drop_predictive_util"
down_revision = "flightsummary_coverage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS api.collect_predictive_utilisation(text, text, timestamptz, timestamptz)")
    op.execute("DROP FUNCTION IF EXISTS api.cleanup_predictive_utilisation()")
    op.execute("DROP TABLE IF EXISTS api.predictive_utilisation")


def downgrade() -> None:
    # Feature intentionally removed; not reversible here (recreating the table + both functions would
    # mean restoring the original predictive_utilisation migrations). No-op.
    pass
