"""main.airports: + keywords column (OurAirports keeps some airports' real IATA/ICAO only here)

Revision ID: airports_keywords
Revises: forecast_icao_cols
Create Date: 2026-07-02

Some OurAirports rows have EMPTY iata_code/icao_code but carry the real codes in the freeform
`keywords` field (e.g. the closed Targovishte airport: keywords "LB16, Bukhovtsi, LBTG, TGV"). The
loader now extracts those into iata/icao; this stores the raw keywords too (reference).
"""
from alembic import op

revision = "airports_keywords"
down_revision = "forecast_icao_cols"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE main.airports ADD COLUMN keywords text")


def downgrade() -> None:
    op.execute("ALTER TABLE main.airports DROP COLUMN IF EXISTS keywords")
