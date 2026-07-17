"""Drop the FlightRadar single-column indexes nobody reads, to speed up the FR24 ingest.

flightradar.flightsummary is 8.3M rows / 4.2 GB and is the FR24 ingest's write target; every index is paid on
every INSERT. pg_stat_user_indexes over a 19-day sample (cluster up since 2026-06-28, i.e. many ingest runs
and many forecast requests) separates the readers from the dead weight:

    KEPT      reg 763,078 scans · orig_iata 291,700 · first_seen 110 · dest_iata 11 · operating_as 8
              · painted_as 1 · created_at 12, plus the composites ix_flightsummary_reg_takeoff (498,765)
              and ix_flightsummary_reg_first_seen, and uq_flightsummary_natural (12.2M).
    DROPPED   0 scans, 803 MB total:
              last_seen 178 MB · datetime_takeoff 173 MB · datetime_landed 172 MB · callsign 60 MB
              · dest_icao_actual 56 MB · orig_icao 56 MB · dest_icao 56 MB · type 52 MB

These are unreachable, not just unpopular:
  * orig_icao / dest_icao / dest_icao_actual appear only inside `nullif(col,'') IS NOT NULL`
    (ForecastAPI/panel.py _assemble_sql) — no btree can serve that predicate.
  * datetime_takeoff / datetime_landed / last_seen are SELECTed and computed with, never filtered; the one
    takeoff lookup that exists is served by the composite ix_flightsummary_reg_takeoff, which stays.
  * callsign is only an FR24 request parameter; the pre-insert dedup filters fr24_id, served by the unique key.

flightradar.livepositions (RANGE-partitioned, the hottest write path — the live poll appends continuously)
gets the same treatment. Scans SUMMED over all partitions, because per-partition counts lie (a freshly created
month always reads 0):
    KEPT      reg_timestamp 54,645 · reg 30,471 · reg_flight_created_at 9,872 · type 6 · operating_as 1
              · the three BRIN indexes (24 kB each — nothing to reclaim, and they serve range scans)
    DROPPED   0 scans everywhere, ~26 MB: callsign · orig_icao · orig_iata · dest_icao · dest_iata · painted_as
The size is small but the point is write amplification: 6 fewer index maintenances on every appended position.

The matching `index=True` was removed from Database/FlightRadarModels.py in ALL THREE copies (db-contract =
source of truth, Core-API app/Database, external-worker worker/Database) — otherwise the next autogenerate
would recreate every one of them.

DROP INDEX CONCURRENTLY (live tables, no ACCESS EXCLUSIVE); it cannot run in a transaction, hence the
autocommit block, and IF EXISTS makes a partial run re-runnable.

Revision ID: fr24_drop_unused_indexes
Revises: forecast_geo_indexes
Create Date: 2026-07-17
"""
from alembic import op

revision = "fr24_drop_unused_indexes"
down_revision = "forecast_geo_indexes"
branch_labels = None
depends_on = None

# index name -> the column it covered (used to rebuild it on downgrade)
_SUMMARY_DROPPED = {
    "ix_flightradar_flightsummary_last_seen": "last_seen",
    "ix_flightradar_flightsummary_datetime_takeoff": "datetime_takeoff",
    "ix_flightradar_flightsummary_datetime_landed": "datetime_landed",
    "ix_flightradar_flightsummary_callsign": "callsign",
    "ix_flightradar_flightsummary_dest_icao_actual": "dest_icao_actual",
    "ix_flightradar_flightsummary_orig_icao": "orig_icao",
    "ix_flightradar_flightsummary_dest_icao": "dest_icao",
    "ix_flightradar_flightsummary_type": "type",
}

# Partitioned parent: dropping the parent index cascades to every partition's copy.
_LIVE_DROPPED = {
    "ix_flightradar_livepositions_callsign": "callsign",
    "ix_flightradar_livepositions_orig_icao": "orig_icao",
    "ix_flightradar_livepositions_orig_iata": "orig_iata",
    "ix_flightradar_livepositions_dest_icao": "dest_icao",
    "ix_flightradar_livepositions_dest_iata": "dest_iata",
    "ix_flightradar_livepositions_painted_as": "painted_as",
}


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for name in _SUMMARY_DROPPED:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS flightradar.{name}")
        # A partitioned table's index cannot be dropped CONCURRENTLY — plain DROP (it is a catalog-only
        # operation on the parent plus each partition's index; brief, and the poll retries).
        for name in _LIVE_DROPPED:
            op.execute(f"DROP INDEX IF EXISTS flightradar.{name}")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, col in _SUMMARY_DROPPED.items():
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} "
                       f"ON flightradar.flightsummary ({col})")
    for name, col in _LIVE_DROPPED.items():
        op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON flightradar.livepositions ({col})")
