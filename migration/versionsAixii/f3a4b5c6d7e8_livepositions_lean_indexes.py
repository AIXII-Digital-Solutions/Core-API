"""livepositions: drop unused gspeed/vspeed/track btrees; swap created_at btree -> BRIN

Revision ID: f3a4b5c6d7e8
Revises: e2f3a4b5c6d7
Create Date: 2026-07-02

livepositions is write-heavy (append-only, a row per active flight every poll) and growing, so it is
kept LEAN: drop the gspeed/vspeed/track btrees (continuous telemetry — not filtered by search;
scans=0; pure write overhead), and replace the created_at btree (unused, MB) with a BRIN index (~KB,
ideal for historical range scans on time-ordered append-only data). All CONCURRENTLY so the live
FR24 ingestion is never blocked.
"""
from alembic import op

revision = "f3a4b5c6d7e8"
down_revision = "e2f3a4b5c6d7"
branch_labels = None
depends_on = None

DROP = [
    "ix_flightradar_livepositions_gspeed",
    "ix_flightradar_livepositions_vspeed",
    "ix_flightradar_livepositions_track",
    "ix_flightradar_livepositions_created_at",  # btree -> replaced by BRIN below
]


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for name in DROP:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS flightradar.{name}")
        op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_livepositions_created_at_brin "
                   "ON flightradar.livepositions USING brin (created_at)")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS flightradar.ix_livepositions_created_at_brin")
        op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_flightradar_livepositions_gspeed "
                   "ON flightradar.livepositions (gspeed)")
        op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_flightradar_livepositions_vspeed "
                   "ON flightradar.livepositions (vspeed)")
        op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_flightradar_livepositions_track "
                   "ON flightradar.livepositions (track)")
        op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_flightradar_livepositions_created_at "
                   "ON flightradar.livepositions (created_at)")
