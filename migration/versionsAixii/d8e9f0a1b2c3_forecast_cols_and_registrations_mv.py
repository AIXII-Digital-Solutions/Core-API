"""forecast: +Contract Year/Circle Distance/Flight Time columns + cirium.registrations matview

Revision ID: d8e9f0a1b2c3
Revises: d7e8f9a0b1c2
Create Date: 2026-07-02

1. Adds three columns to forecast.history_1 / future_1 / final_1:
     "Contract Year"   text    — fiscal year of the row's Date relative to the REQUEST date's
                                 month/day (pipeline computes it): the 12-month window [anchor, anchor+1y)
                                 that contains the Date, labelled by its START year (e.g. anchor 2026-07-01:
                                 a 2025-08 flight -> CY2025, a 2025-03 flight -> CY2024).
     "Circle Distance" double precision — great-circle origin->destination distance (from FR24).
     "Flight Time"     interval — Time Landed - Time Departed.

2. cirium.registrations — MATERIALIZED VIEW, one row per unique Registration holding its LATEST
   Operator + Status (DISTINCT ON scanning revisions newest-first = "from the end"). Mirrors
   cirium.airlines; refreshed by external-worker alongside the other cirium matviews.
"""
from alembic import op

revision = "d8e9f0a1b2c3"
down_revision = "d7e8f9a0b1c2"
branch_labels = None
depends_on = None

_TABLES = ("history_1", "future_1", "final_1")

_NEW_COLS = (
    ('"Contract Year"', "text"),
    ('"Circle Distance"', "double precision"),
    ('"Flight Time"', "interval"),
)

REGISTRATIONS_MV = """
CREATE MATERIALIZED VIEW cirium.registrations AS
SELECT DISTINCT ON ("Registration")
    "Registration" AS registration,
    "Operator"     AS operator,
    "Status"       AS status
FROM cirium.ciriumaircrafts
WHERE "Registration" IS NOT NULL
ORDER BY "Registration", revision_id DESC
WITH DATA
"""


def upgrade() -> None:
    for tbl in _TABLES:
        for col, typ in _NEW_COLS:
            op.execute(f'ALTER TABLE forecast.{tbl} ADD COLUMN {col} {typ}')

    op.execute(REGISTRATIONS_MV)
    # unique index -> enables REFRESH ... CONCURRENTLY; also the registration lookup/search key
    op.execute('CREATE UNIQUE INDEX ix_cirium_registrations_registration '
               'ON cirium.registrations (registration)')
    op.execute('CREATE INDEX ix_cirium_registrations_status ON cirium.registrations (status)')


def downgrade() -> None:
    op.execute("DROP MATERIALIZED VIEW IF EXISTS cirium.registrations")
    for tbl in _TABLES:
        for col, _typ in _NEW_COLS:
            op.execute(f'ALTER TABLE forecast.{tbl} DROP COLUMN IF EXISTS {col}')
