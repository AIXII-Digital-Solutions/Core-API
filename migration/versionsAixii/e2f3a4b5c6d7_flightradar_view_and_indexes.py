"""flightradar: current_positions view + search indexes on flightsummary / livepositions

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-07-02

flightradar.current_positions VIEW: the latest livepositions row per aircraft (reg) — "current
flight" for aircraft in the air, or the last known row for ones long out of coverage. Implemented as
a skip-scan (recursive reg walk + LATERAL "ORDER BY timestamp DESC LIMIT 1"), so it reads ~one index
row per distinct reg regardless of how big the append-only livepositions table grows (2 ms vs 150 ms
for a naive DISTINCT ON). Adds is_grounded: FALSE only when the telemetry is fresh (<15 min) AND the
aircraft is moving at flight speed (gspeed > 50) — i.e. genuinely airborne now; TRUE otherwise
(on the ground, or stale/not currently tracked).

Search indexes for the common filter fields on the two big tables (matches the model index=True /
Index() declarations). Built CONCURRENTLY (autocommit) so the live FR24 ingestion into flightsummary
(~8M rows) / livepositions is not blocked. airports (~1k) / airportrunways (~3k) are tiny — no extra
indexes (a seq scan is instant; icao/iata/airport_id are already unique-indexed).
"""
from alembic import op

revision = "e2f3a4b5c6d7"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None

# (index_name, table, columns-SQL) — names match the model's index=True / Index() declarations.
INDEXES = [
    ("ix_flightradar_flightsummary_callsign", "flightsummary", "callsign"),
    ("ix_flightradar_flightsummary_datetime_takeoff", "flightsummary", "datetime_takeoff"),
    ("ix_flightradar_flightsummary_datetime_landed", "flightsummary", "datetime_landed"),
    ("ix_flightradar_flightsummary_first_seen", "flightsummary", "first_seen"),
    ("ix_flightradar_flightsummary_last_seen", "flightsummary", "last_seen"),
    ("ix_flightradar_flightsummary_created_at", "flightsummary", "created_at"),
    ("ix_flightradar_livepositions_callsign", "livepositions", "callsign"),
    ("ix_flightradar_livepositions_gspeed", "livepositions", "gspeed"),
    ("ix_flightradar_livepositions_vspeed", "livepositions", "vspeed"),
    ("ix_flightradar_livepositions_timestamp", "livepositions", '"timestamp"'),
    ("ix_flightradar_livepositions_type", "livepositions", "type"),
    ("ix_flightradar_livepositions_track", "livepositions", "track"),
    ("ix_flightradar_livepositions_created_at", "livepositions", "created_at"),
    ("ix_livepositions_reg_timestamp", "livepositions", 'reg, "timestamp"'),
]

VIEW_SQL = """
CREATE VIEW flightradar.current_positions AS
WITH RECURSIVE regs AS (
    (SELECT reg FROM flightradar.livepositions WHERE reg IS NOT NULL ORDER BY reg LIMIT 1)
    UNION ALL
    SELECT (SELECT reg FROM flightradar.livepositions
            WHERE reg > r.reg AND reg IS NOT NULL ORDER BY reg LIMIT 1)
    FROM regs r WHERE r.reg IS NOT NULL
)
SELECT lp.*,
       NOT (lp.timestamp >= now() - interval '15 minutes' AND coalesce(lp.gspeed, 0) > 50) AS is_grounded
FROM regs
CROSS JOIN LATERAL (
    SELECT * FROM flightradar.livepositions l
    WHERE l.reg = regs.reg
    ORDER BY l.timestamp DESC
    LIMIT 1
) lp
WHERE regs.reg IS NOT NULL
"""


def upgrade() -> None:
    # CONCURRENTLY needs autocommit (can't run inside the migration's transaction) and avoids
    # blocking the live ingestion on flightsummary / livepositions while the indexes build.
    with op.get_context().autocommit_block():
        for name, table, cols in INDEXES:
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} "
                       f"ON flightradar.{table} ({cols})")
    op.execute(VIEW_SQL)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS flightradar.current_positions")
    with op.get_context().autocommit_block():
        for name, _table, _cols in INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS flightradar.{name}")
