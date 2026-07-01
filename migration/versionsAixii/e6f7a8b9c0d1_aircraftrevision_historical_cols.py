"""cirium.aircraftrevision: is_historical / period / plan_type columns

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-06-30

Adds back-fill metadata to cirium.aircraftrevision so the one-off historical Cirium load
(_admin/load_historical_cirium.py) can tag revisions:
- is_historical : TRUE for the manually loaded 2022-2025 monthly snapshots, FALSE for live
  revisions written by file-processor (server_default false -> existing rows back-filled to false).
- period        : "MM-YYYY" of the snapshot (e.g. "05-2025"); NULL for live revisions.
- plan_type     : "Commercial" | "Business&Helicopters" (which source folder); NULL for live
  revisions. Mirrors the per-row cirium.ciriumaircrafts.plan_type the loader also fills.

Schema-only. The actual back-fill + revision renumbering is done by the admin script, not here.
"""
from alembic import op
import sqlalchemy as sa

revision = "e6f7a8b9c0d1"
down_revision = "d5e6f7a8b9c0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "aircraftrevision",
        sa.Column("is_historical", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        schema="cirium",
    )
    op.add_column(
        "aircraftrevision",
        sa.Column("period", sa.String(), nullable=True),
        schema="cirium",
    )
    op.add_column(
        "aircraftrevision",
        sa.Column("plan_type", sa.String(), nullable=True),
        schema="cirium",
    )


def downgrade() -> None:
    op.drop_column("aircraftrevision", "plan_type", schema="cirium")
    op.drop_column("aircraftrevision", "period", schema="cirium")
    op.drop_column("aircraftrevision", "is_historical", schema="cirium")
