"""cirium.airlines: drop airlines whose name starts with a digit AND have no ICAO/IATA

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-06-28

Rebuild the cirium.airlines materialized view with an extra filter: exclude operators whose name
starts with a digit and that carry NEITHER an ICAO nor an IATA code (noise rows). Digit-named
operators that DO have a code are kept (the DISTINCT ON already prefers the coded row).
"""
from alembic import op

revision = "c4d5e6f7a8b9"
down_revision = "b3c4d5e6f7a8"
branch_labels = None
depends_on = None

_INDEXES = [
    'CREATE UNIQUE INDEX ix_cirium_airlines_airline ON cirium.airlines (airline)',
    'CREATE INDEX ix_cirium_airlines_icao ON cirium.airlines (icao)',
    'CREATE INDEX ix_cirium_airlines_iata ON cirium.airlines (iata)',
]

NEW_VIEW = """
CREATE MATERIALIZED VIEW cirium.airlines AS
SELECT DISTINCT ON ("Operator")
    "Operator"      AS airline,
    "Operator ICAO" AS icao,
    "Operator IATA" AS iata
FROM cirium.ciriumaircrafts
WHERE "Operator" IS NOT NULL
  AND NOT ("Operator" ~ '^[0-9]' AND "Operator ICAO" IS NULL AND "Operator IATA" IS NULL)
ORDER BY "Operator", ("Operator ICAO" IS NULL), ("Operator IATA" IS NULL)
WITH DATA
"""

OLD_VIEW = """
CREATE MATERIALIZED VIEW cirium.airlines AS
SELECT DISTINCT ON ("Operator")
    "Operator"      AS airline,
    "Operator ICAO" AS icao,
    "Operator IATA" AS iata
FROM cirium.ciriumaircrafts
WHERE "Operator" IS NOT NULL
ORDER BY "Operator", ("Operator ICAO" IS NULL), ("Operator IATA" IS NULL)
WITH DATA
"""


def upgrade() -> None:
    op.execute("DROP MATERIALIZED VIEW IF EXISTS cirium.airlines")
    op.execute(NEW_VIEW)
    for ix in _INDEXES:
        op.execute(ix)


def downgrade() -> None:
    op.execute("DROP MATERIALIZED VIEW IF EXISTS cirium.airlines")
    op.execute(OLD_VIEW)
    for ix in _INDEXES:
        op.execute(ix)
