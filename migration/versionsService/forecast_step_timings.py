"""add forecast_step_timings (progress/ETA self-calibration ledger)

Revision ID: forecast_step_timings
Revises: ef8d7515b7d3
Create Date: 2026-07-09
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "forecast_step_timings"
down_revision: Union[str, Sequence[str], None] = "ef8d7515b7d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "forecast_step_timings",
        sa.Column("step", sa.String(length=32), nullable=False),
        sa.Column("duration_s", sa.Float(), nullable=False),
        sa.Column("units", sa.Float(), nullable=True),
        sa.Column("context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_forecast_step_timings_step"), "forecast_step_timings", ["step"], unique=False)
    op.create_index("ix_forecast_step_timings_step_created", "forecast_step_timings", ["step", "created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_forecast_step_timings_step_created", table_name="forecast_step_timings")
    op.drop_index(op.f("ix_forecast_step_timings_step"), table_name="forecast_step_timings")
    op.drop_table("forecast_step_timings")
