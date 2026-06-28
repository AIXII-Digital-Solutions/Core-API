"""create api.registration (active aircraft from cirium.asg) + sync function

Revision ID: e0f1a2b3c4d5
Revises: d9e0f1a2b3c4
Create Date: 2026-06-28

`api.registration` holds the currently active aircraft (one row per cirium.asg row with
is_active = true): reg (Registration), msn (Serial Number) and airline_id -> api.airlines.

It is NOT hand-maintained. `api.sync_registration_from_asg()` rebuilds it (TRUNCATE + INSERT,
joining api.airlines on the airline name asg matched). external-worker calls this function right
after every `REFRESH MATERIALIZED VIEW cirium.asg`. The initial population runs here so the table
is non-empty immediately. Forward-only.
"""
from alembic import op
import sqlalchemy as sa

revision = "e0f1a2b3c4d5"
down_revision = "d9e0f1a2b3c4"
branch_labels = None
depends_on = None


SYNC_FN = """
CREATE OR REPLACE FUNCTION api.sync_registration_from_asg() RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    TRUNCATE api.registration RESTART IDENTITY;
    INSERT INTO api.registration (reg, msn, airline_id)
    SELECT a."Registration", a."Serial Number", al.id
    FROM cirium.asg a
    LEFT JOIN api.airlines al ON al.airline_name = a."Airline"
    WHERE a.is_active;
END;
$$;
"""


def upgrade() -> None:
    op.create_table(
        "registration",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("reg", sa.String(), nullable=False),
        sa.Column("msn", sa.String(), nullable=True),
        sa.Column(
            "airline_id", sa.BigInteger(),
            sa.ForeignKey("api.airlines.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        schema="api",
    )
    op.create_index("ix_registration_reg", "registration", ["reg"], schema="api")
    op.create_index("ix_registration_msn", "registration", ["msn"], schema="api")
    op.create_index("ix_registration_airline_id", "registration", ["airline_id"], schema="api")

    op.execute(SYNC_FN)
    op.execute("SELECT api.sync_registration_from_asg()")  # initial population from current asg


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS api.sync_registration_from_asg()")
    op.drop_table("registration", schema="api")
