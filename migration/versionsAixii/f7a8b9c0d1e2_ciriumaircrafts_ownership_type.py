"""cirium.ciriumaircrafts: add "Ownership Type" column

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-07-01

The Business&Helicopters Cirium export carries an "Ownership Type" column that the Commercial export
does not, and which had no home in cirium.ciriumaircrafts. Add it (nullable String) so the historical
back-fill (_admin/load_historical_cirium.py) loads it instead of silently dropping it. Live Commercial
loads simply leave it NULL.

NOTE: worker repos that vendor the CiriumAircrafts model (e.g. file-processor) should sync this column;
it is nullable, so their inserts keep working without the change.
"""
from alembic import op
import sqlalchemy as sa

revision = "f7a8b9c0d1e2"
down_revision = "e6f7a8b9c0d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ciriumaircrafts",
        sa.Column("Ownership Type", sa.String(), nullable=True),
        schema="cirium",
    )


def downgrade() -> None:
    op.drop_column("ciriumaircrafts", "Ownership Type", schema="cirium")
