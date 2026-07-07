"""main.airports_ref: include ALL OurAirports (not just IATA-having) — iata nullable, ident key

Revision ID: airports_ref_all
Revises: oa_airports_ref
Create Date: 2026-07-02

Widen main.airports_ref to hold EVERY OurAirports airport (~85k), including the ones with no IATA
code (ICAO-only / small airfields / heliports). iata becomes nullable; the unique key moves to
`ident` (OurAirports's always-present identifier). iata keeps a PARTIAL unique index (WHERE iata IS
NOT NULL) so the forecast's by-IATA join stays unique+fast while many null-iata rows coexist.
"""
from alembic import op

revision = "airports_ref_all"
down_revision = "oa_airports_ref"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute('ALTER TABLE main.airports_ref ADD COLUMN ident text')
    op.execute('ALTER TABLE main.airports_ref ALTER COLUMN iata DROP NOT NULL')
    op.execute('DROP INDEX IF EXISTS main.ix_airports_ref_iata')
    op.execute('CREATE UNIQUE INDEX ix_airports_ref_iata ON main.airports_ref (iata) WHERE iata IS NOT NULL')
    op.execute('CREATE UNIQUE INDEX ix_airports_ref_ident ON main.airports_ref (ident)')


def downgrade() -> None:
    op.execute('DROP INDEX IF EXISTS main.ix_airports_ref_ident')
    op.execute('DROP INDEX IF EXISTS main.ix_airports_ref_iata')
    op.execute('CREATE UNIQUE INDEX ix_airports_ref_iata ON main.airports_ref (iata)')
    op.execute('ALTER TABLE main.airports_ref DROP COLUMN IF EXISTS ident')
