"""flightradar: current_positions_flow view (per-flight chronology) + smarter is_grounded

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
Create Date: 2026-07-02

Two things, driven by an analysis of flightradar.livepositions (410k rows, 188 regs, ~10-min
telemetry cadence, no NULLs in alt/gspeed/timestamp/reg/fr24_id):

1. A NEW read-only view **flightradar.current_positions_flow** — the FULL chronology of each
   aircraft's current (if flying) / last (if landed) flight, one row per telemetry point (not one row
   per aircraft like current_positions). A "flight" is a fr24_id (verified globally unique: no fr24_id
   is shared by >1 reg), so per reg we pick the fr24_id of its most recent position (skip-scan over
   distinct reg, same trick as current_positions) and return every livepositions row of that fr24_id,
   ordered by time. This gives the ground->air->ground track of the tail's current/last flight.

2. A smarter **is_grounded**, applied to BOTH views. The old rule ("fresh <15 min AND gspeed>50")
   was a coarse snapshot flag. The data shows a clean split:
     - on-ground cluster: alt<500 AND gspeed<50  -> 59,639 rows (parked/taxi; max gs 49, max alt 475)
     - airborne cluster:  alt>=2000 AND gspeed>=100 -> 340,261 rows (cruise)
   with a clear gap between them. And telemetry regularly DROPS OUT near landing (a flight's last
   point is often mid-descent at a few thousand ft, then silence). Hence three ways to be grounded:

       is_grounded = (alt < 500 AND gspeed < 50)              -- physically on the ground
                     OR (no data for > 2 hours)               -- stale in any state -> assume landed
                     OR (no data for > 30 min                 -- went silent shortly after being seen
                         AND alt < 10000 AND vspeed < -256)   -- ...on a terminal-area descent = landed

   The third term is the important one: ~35% of flights are last seen DESCENDING below 10,000 ft
   (on approach) and then go silent because they dropped below coverage on landing. Waiting the full
   2 h to call those grounded is wrong — 30 min after the last ping is plenty. alt<10000 excludes
   cruise step-downs (which happen at FL); vspeed<-256 excludes climb-outs and level flight; and the
   30-min grace sits safely ABOVE the ~20-min (p90) poll cadence, so an aircraft actively tracked on
   approach (fresh points still arriving) is NOT grounded between two normal points — only once the
   stream actually stops for 30 min.

   * current_positions (one latest row per reg): the gap is `now() - timestamp`.
   * current_positions_flow (per-row over a flight's chronology): the gap is measured to the NEXT
     point of the same flight — `lead(timestamp) - timestamp` — and for the final point that is
     `now() - timestamp`. So a completed flight's trailing (data-cutoff) point flips to grounded even
     if it was still airborne when the signal died, while an actively-tracked flight's fresh last
     point stays airborne. Net effect over a full flight: takeoff rows grounded (alt~0), cruise rows
     airborne, landing/last row grounded again.

Thresholds (500 ft / 50 kt / 10000 ft / -256 fpm / 30 min / 2 h) are inlined here and easy to tune.
Both views live on FlightRadarViewBase (out of the aixii Alembic target), so autogenerate never
touches them.
"""
from alembic import op

revision = "c6d7e8f9a0b1"
down_revision = "b5c6d7e8f9a0"
branch_labels = None
depends_on = None


# Terminal-area descent that then goes silent = landed. alt<10000 excludes cruise step-downs (at FL);
# vspeed<-256 fpm excludes climb-outs and level flight (vspeed comes in multiples of 64; -256 clears
# the ±64/±128 level-flight noise). Paired with a 30-min "no data" grace in each is_grounded below.
_DESCENT = "lp.alt < 10000 AND coalesce(lp.vspeed, 0) < -256"

# gap from a point to the NEXT telemetry point of the same flight (now() for the final point).
_GAP_AFTER = ("coalesce(lead(lp.timestamp) OVER (PARTITION BY lp.fr24_id ORDER BY lp.timestamp), now()) "
              "- lp.timestamp")

# is_grounded for the single-row snapshot (current_positions): on the ground, OR stale > 2h, OR last
# seen on a terminal descent and then silent > 30 min (landed without waiting the full 2h).
_IS_GROUNDED_SNAPSHOT = (
    "(coalesce(lp.alt, 0) < 500 AND coalesce(lp.gspeed, 0) < 50) "
    "OR (lp.timestamp < now() - interval '2 hours') "
    f"OR (lp.timestamp < now() - interval '30 minutes' AND {_DESCENT})"
)

# is_grounded per point of a flight's chronology (current_positions_flow): same three rules, but the
# "no data" gap is measured to the NEXT point of the same flight (now() for the last point).
_IS_GROUNDED_FLOW = (
    "(coalesce(lp.alt, 0) < 500 AND coalesce(lp.gspeed, 0) < 50) "
    f"OR ({_GAP_AFTER} > interval '2 hours') "
    f"OR ({_GAP_AFTER} > interval '30 minutes' AND {_DESCENT})"
)

# The recursive distinct-reg skip-scan reused by both views (reads ~1 index row per reg).
_REG_WALK = """
WITH RECURSIVE regs AS (
    (SELECT reg FROM flightradar.livepositions WHERE reg IS NOT NULL ORDER BY reg LIMIT 1)
    UNION ALL
    SELECT (SELECT reg FROM flightradar.livepositions
            WHERE reg > r.reg AND reg IS NOT NULL ORDER BY reg LIMIT 1)
    FROM regs r WHERE r.reg IS NOT NULL
)"""

# current_positions (unchanged shape: latest livepositions row per reg + is_grounded) — only the
# is_grounded expression is upgraded, so CREATE OR REPLACE is safe (same columns / types / order).
CURRENT_POSITIONS_SQL = f"""
CREATE OR REPLACE VIEW flightradar.current_positions AS
{_REG_WALK}
SELECT lp.*,
       {_IS_GROUNDED_SNAPSHOT} AS is_grounded
FROM regs
CROSS JOIN LATERAL (
    SELECT * FROM flightradar.livepositions l
    WHERE l.reg = regs.reg
    ORDER BY l.timestamp DESC
    LIMIT 1
) lp
WHERE regs.reg IS NOT NULL
"""

# Old definition, restored on downgrade.
CURRENT_POSITIONS_SQL_OLD = f"""
CREATE OR REPLACE VIEW flightradar.current_positions AS
{_REG_WALK}
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

# current_positions_flow: per reg, the fr24_id of its latest position (= current/last flight), then
# every livepositions row of that flight, chronological, each with per-point is_grounded.
CURRENT_POSITIONS_FLOW_SQL = f"""
CREATE VIEW flightradar.current_positions_flow AS
{_REG_WALK},
latest_flight AS (
    SELECT regs.reg, lf.fr24_id
    FROM regs
    CROSS JOIN LATERAL (
        SELECT l.fr24_id FROM flightradar.livepositions l
        WHERE l.reg = regs.reg
        ORDER BY l.timestamp DESC
        LIMIT 1
    ) lf
    WHERE regs.reg IS NOT NULL AND lf.fr24_id IS NOT NULL
)
SELECT lp.*,
       {_IS_GROUNDED_FLOW} AS is_grounded
FROM latest_flight lff
JOIN flightradar.livepositions lp
  ON lp.reg = lff.reg AND lp.fr24_id = lff.fr24_id
ORDER BY lp.reg, lp.timestamp
"""


def upgrade() -> None:
    op.execute(CURRENT_POSITIONS_SQL)          # upgrade is_grounded on the snapshot view
    op.execute(CURRENT_POSITIONS_FLOW_SQL)     # new chronology view


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS flightradar.current_positions_flow")
    op.execute(CURRENT_POSITIONS_SQL_OLD)      # restore the old is_grounded expression
