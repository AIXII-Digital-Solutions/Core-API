"""flightradar.livepositions -> monthly RANGE-partitioned by timestamp (+ BRIN timestamp/eta)

Revision ID: a4b5c6d7e8f9
Revises: f3a4b5c6d7e8
Create Date: 2026-07-02

Convert the append-only livepositions into a monthly RANGE-partitioned table (key = timestamp) so it
stays manageable as history grows forever: the hot (current-month) partition stays small (fast
inserts / current_positions view), VACUUM only touches live partitions, old months sit in-place, and
time-range queries prune to the relevant partitions. Done while the table is small (~0.4M rows) so
the one-shot copy+swap is trivial and fully transactional (ACCESS EXCLUSIVE lock freezes the FR24
poll for the few seconds it takes).

Key decisions (see discussion): partition key = timestamp (NOT created_at) because the dedup
ON CONFLICT relies on UNIQUE(fr24_id, timestamp) and a partitioned table's UNIQUE/PK must contain the
partition key — so PK becomes (id, timestamp), UNIQUE(fr24_id, timestamp) stays intact, ON CONFLICT
keeps working. timestamp is NOT NULL (verified 0 nulls). A DEFAULT partition catches any out-of-range
row. Also swaps the standalone timestamp btree for BRIN (timestamp is the partition key -> range is
pruned at partition level; BRIN is ~KB) and adds a BRIN on eta.
"""
from alembic import op

revision = "a4b5c6d7e8f9"
down_revision = "f3a4b5c6d7e8"
branch_labels = None
depends_on = None

SCHEMA = "flightradar"
SEQ = "flightradar.liveposition_id_seq"

# secondary indexes to (re)build on the partitioned table. timestamp -> BRIN, + created_at/eta BRIN.
IDX = [
    ("ix_flightradar_livepositions_callsign", "btree", "callsign"),
    ("ix_flightradar_livepositions_dest_iata", "btree", "dest_iata"),
    ("ix_flightradar_livepositions_dest_icao", "btree", "dest_icao"),
    ("ix_flightradar_livepositions_operating_as", "btree", "operating_as"),
    ("ix_flightradar_livepositions_orig_iata", "btree", "orig_iata"),
    ("ix_flightradar_livepositions_orig_icao", "btree", "orig_icao"),
    ("ix_flightradar_livepositions_painted_as", "btree", "painted_as"),
    ("ix_flightradar_livepositions_reg", "btree", "reg"),
    ("ix_flightradar_livepositions_type", "btree", "type"),
    ("ix_livepositions_reg_flight_created", "btree", "reg, flight, created_at"),
    ("ix_livepositions_reg_timestamp", "btree", 'reg, "timestamp"'),
    ("ix_livepositions_created_at_brin", "brin", "created_at"),
    ("ix_livepositions_timestamp_brin", "brin", '"timestamp"'),
    ("ix_livepositions_eta_brin", "brin", "eta"),
]

# monthly partitions: existing data (2026-01..2026-07) + a small future buffer. DEFAULT catches the rest.
_MONTHS = [(2026, m) for m in range(1, 10)]  # 2026-01 .. 2026-09

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
    WHERE l.reg = regs.reg ORDER BY l.timestamp DESC LIMIT 1
) lp
WHERE regs.reg IS NOT NULL
"""


def _month_bounds(y, m):
    ny, nm = (y, m + 1) if m < 12 else (y + 1, 1)
    return f"{y}-{m:02d}-01 00:00:00+00", f"{ny}-{nm:02d}-01 00:00:00+00"


def upgrade() -> None:
    # freeze the live table so the FR24 poll cannot insert between the copy and the swap
    op.execute(f"LOCK TABLE {SCHEMA}.livepositions IN ACCESS EXCLUSIVE MODE")

    op.execute(f"CREATE TABLE {SCHEMA}.livepositions_new "
               f"(LIKE {SCHEMA}.livepositions INCLUDING DEFAULTS INCLUDING GENERATED INCLUDING STORAGE) "
               f'PARTITION BY RANGE ("timestamp")')
    op.execute(f'ALTER TABLE {SCHEMA}.livepositions_new ALTER COLUMN "timestamp" SET NOT NULL')
    op.execute(f'ALTER TABLE {SCHEMA}.livepositions_new '
               f'ADD CONSTRAINT livepositions_pkey_tmp PRIMARY KEY ("timestamp", id)')
    op.execute(f'ALTER TABLE {SCHEMA}.livepositions_new '
               f'ADD CONSTRAINT uq_livepositions_fr24_timestamp_tmp UNIQUE (fr24_id, "timestamp")')
    for name, using, cols in IDX:
        op.execute(f"CREATE INDEX {name}_tmp ON {SCHEMA}.livepositions_new USING {using} ({cols})")

    for y, m in _MONTHS:
        a, b = _month_bounds(y, m)
        op.execute(f"CREATE TABLE {SCHEMA}.livepositions_{y}_{m:02d} "
                   f"PARTITION OF {SCHEMA}.livepositions_new FOR VALUES FROM ('{a}') TO ('{b}')")
    op.execute(f"CREATE TABLE {SCHEMA}.livepositions_default PARTITION OF {SCHEMA}.livepositions_new DEFAULT")

    op.execute(f"INSERT INTO {SCHEMA}.livepositions_new SELECT * FROM {SCHEMA}.livepositions")

    # keep the id sequence alive across the DROP of the old table, then swap in the new one
    op.execute(f"ALTER SEQUENCE {SEQ} OWNED BY NONE")
    op.execute(f"DROP VIEW {SCHEMA}.current_positions")
    op.execute(f"DROP TABLE {SCHEMA}.livepositions")
    op.execute(f"ALTER TABLE {SCHEMA}.livepositions_new RENAME TO livepositions")
    op.execute(f"ALTER TABLE {SCHEMA}.livepositions RENAME CONSTRAINT livepositions_pkey_tmp TO livepositions_pkey")
    op.execute(f"ALTER TABLE {SCHEMA}.livepositions "
               f"RENAME CONSTRAINT uq_livepositions_fr24_timestamp_tmp TO uq_livepositions_fr24_timestamp")
    for name, _using, _cols in IDX:
        op.execute(f"ALTER INDEX {SCHEMA}.{name}_tmp RENAME TO {name}")
    op.execute(f"ALTER SEQUENCE {SEQ} OWNED BY {SCHEMA}.livepositions.id")
    op.execute(f"SELECT setval('{SEQ}', (SELECT COALESCE(max(id), 1) FROM {SCHEMA}.livepositions))")

    op.execute(VIEW_SQL)


def downgrade() -> None:
    # reverse: collapse the partitioned table back into a plain one
    op.execute(f"LOCK TABLE {SCHEMA}.livepositions IN ACCESS EXCLUSIVE MODE")
    op.execute(f"CREATE TABLE {SCHEMA}.livepositions_plain "
               f"(LIKE {SCHEMA}.livepositions INCLUDING DEFAULTS INCLUDING GENERATED INCLUDING STORAGE)")
    op.execute(f'ALTER TABLE {SCHEMA}.livepositions_plain ADD CONSTRAINT liveposition_pkey_tmp PRIMARY KEY (id)')
    op.execute(f'ALTER TABLE {SCHEMA}.livepositions_plain '
               f'ADD CONSTRAINT uq_livepositions_fr24_timestamp_tmp UNIQUE (fr24_id, "timestamp")')
    op.execute(f"INSERT INTO {SCHEMA}.livepositions_plain SELECT * FROM {SCHEMA}.livepositions")
    op.execute(f"ALTER SEQUENCE {SEQ} OWNED BY NONE")
    op.execute(f"DROP VIEW {SCHEMA}.current_positions")
    op.execute(f"DROP TABLE {SCHEMA}.livepositions")  # drops partitions too
    op.execute(f"ALTER TABLE {SCHEMA}.livepositions_plain RENAME TO livepositions")
    op.execute(f"ALTER TABLE {SCHEMA}.livepositions RENAME CONSTRAINT liveposition_pkey_tmp TO liveposition_pkey")
    op.execute(f"ALTER TABLE {SCHEMA}.livepositions "
               f"RENAME CONSTRAINT uq_livepositions_fr24_timestamp_tmp TO uq_livepositions_fr24_timestamp")
    for name, using, cols in IDX:
        op.execute(f"CREATE INDEX {name} ON {SCHEMA}.livepositions USING {using} ({cols})")
    op.execute(f"ALTER SEQUENCE {SEQ} OWNED BY {SCHEMA}.livepositions.id")
    op.execute(VIEW_SQL)
