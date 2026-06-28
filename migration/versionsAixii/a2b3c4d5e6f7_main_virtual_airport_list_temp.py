"""TEMP: create schema main + main.virtual_airport_list (legacy airport coords)

Revision ID: a2b3c4d5e6f7
Revises: f1a2b3c4d5e6
Create Date: 2026-06-28

TEMPORARY stopgap until the `main`/core domain is rebuilt. external-worker's distance calculation
looks up airport coordinates by IATA from `virtual_airport_list`, which used to live in the old
`ai12_main` database (18k-row airport reference). The consolidation dropped it, so we re-create the
table under a new `main` schema in the aixii DB and copy the data in (see _admin ETL).

When core is rebuilt this schema/table should be replaced by a proper airport reference (or the
distance code repointed). The table is a plain reference list (no id/timestamps), keyed by IATA.
"""
from alembic import op
import sqlalchemy as sa

revision = "a2b3c4d5e6f7"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS main")
    op.create_table(
        "virtual_airport_list",
        sa.Column("IATA Code", sa.Text()),
        sa.Column("Airport Name", sa.Text()),
        sa.Column("City", sa.Text()),
        sa.Column("Country", sa.Text()),
        sa.Column("Country Code", sa.Text()),
        sa.Column("Region", sa.Text()),
        sa.Column("Greater Region", sa.Text()),
        sa.Column("Latitude", sa.Double()),
        sa.Column("Longitude", sa.Double()),
        sa.Column("Long/Lat", sa.Text()),
        schema="main",
    )
    op.create_index(
        "ix_virtual_airport_list_iata", "virtual_airport_list", ["IATA Code"], schema="main"
    )


def downgrade() -> None:
    op.drop_index("ix_virtual_airport_list_iata", "virtual_airport_list", schema="main")
    op.drop_table("virtual_airport_list", schema="main")
    # leave the (now-empty) main schema in place — core rebuild will own it.
