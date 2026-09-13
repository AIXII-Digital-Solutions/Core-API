"""Keep the last month of forecast runs: forecast.acys_snapshots + forecast.acys_snapshot_rows.

forecast.acys_summary_by_day holds exactly ONE run — every request TRUNCATEs it and rebuilds it, so
the previous run's dataset is gone the moment the next one starts. These two tables are the short
history of that table: every successful run copies its finished dataset here, and a run older than
the retention window (external-worker's FORECAST_SNAPSHOT_RETENTION_DAYS, 30 days) is pruned at the
end of the next run. A stored snapshot can be poured back into acys_summary_by_day by the
`forecast_restore` job (core-api: POST /forecast/ with `snapshot_id`) — no fetch, no model run, just
the rows and the matview refresh that follows them.

  * acys_snapshots      — one row per run: WHEN, WHAT WAS ASKED FOR (operators / registrations /
                          as_of / profile) and WHAT THE DATASET ACTUALLY CONTAINS (covered_operators /
                          covered_registrations, read back from the rows). Both pairs are kept because
                          they answer different questions: an operator-scoped run names no tails, yet
                          its dataset holds every tail of that operator — and "show me the runs that
                          cover N123AB" must find it. The listing endpoint filters on the union.
  * acys_snapshot_rows  — the dataset itself, snapshot_id + a `LIKE acys_summary_by_day` copy of its
                          columns. Two deliberate choices in that LIKE:
                          NOT INCLUDING DEFAULTS — `id` must NOT inherit the nextval() of the live
                          table's sequence (a snapshot copies the source id, it does not mint new ones
                          from a sequence the live table is still using);
                          INCLUDING GENERATED — the four generated columns ("Origin City&Country",
                          "Destination City&Country", "MERGED_KEY", "DateInt") stay GENERATED here
                          rather than becoming plain copies. A generated column cannot be written to,
                          so both directions of the copy skip them and both tables compute them from
                          the columns that ARE copied — which is also four fewer columns to store.

DRIFT: a column added to acys_summary_by_day is NOT added here automatically — add it in the same
migration. The worker copies the INTERSECTION of the two column lists and logs a warning naming
anything it had to skip, so drift degrades the snapshot rather than breaking the run.

SIZE: one run is ~1.2M rows / ~0.5 GB today. Thirty days of runs is the price of being able to
re-show any of them without re-running the model; the retention window is what bounds it.

No GIN indexes on the scope arrays on purpose: the header table holds a few hundred rows per month
and the listing matches case-insensitively (unnest + lower), which no array index would serve anyway.

Revision ID: forecast_run_snapshots
Revises: insurance_schema_move
Create Date: 2026-09-13
"""
from alembic import op

revision = "forecast_run_snapshots"
down_revision = "insurance_schema_move"
branch_labels = None
depends_on = None

_CREATE_HEADER = r"""
CREATE TABLE forecast.acys_snapshots (
    id                    bigserial PRIMARY KEY,
    created_at            timestamptz NOT NULL DEFAULT now(),
    job_id                text,
    request_type          text        NOT NULL DEFAULT 'ACYS',
    operators             text[]      NOT NULL DEFAULT '{}'::text[],
    registrations         text[]      NOT NULL DEFAULT '{}'::text[],
    covered_operators     text[]      NOT NULL DEFAULT '{}'::text[],
    covered_registrations text[]      NOT NULL DEFAULT '{}'::text[],
    as_of                 date,
    profile               text,
    row_count             bigint      NOT NULL DEFAULT 0,
    restored_at           timestamptz,
    restore_count         integer     NOT NULL DEFAULT 0
)
"""

_CREATE_ROWS = r"""
CREATE TABLE forecast.acys_snapshot_rows (
    snapshot_id bigint NOT NULL
        REFERENCES forecast.acys_snapshots(id) ON DELETE CASCADE,
    LIKE forecast.acys_summary_by_day INCLUDING GENERATED
)
"""

# (snapshot_id, id) is both the uniqueness guarantee and the only access path this table has:
# everything reads it as "all rows of ONE snapshot", so no separate index on snapshot_id is needed.
_PKEY = ("ALTER TABLE forecast.acys_snapshot_rows "
         "ADD CONSTRAINT acys_snapshot_rows_pkey PRIMARY KEY (snapshot_id, id)")

_GRANTS = r"""
DO $do$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aviation_write') THEN
        EXECUTE 'GRANT SELECT,INSERT,UPDATE,DELETE,TRUNCATE ON forecast.acys_snapshots, forecast.acys_snapshot_rows TO grp_aviation_write';
        EXECUTE 'GRANT USAGE,SELECT ON SEQUENCE forecast.acys_snapshots_id_seq TO grp_aviation_write';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aixii_read') THEN
        EXECUTE 'GRANT SELECT ON forecast.acys_snapshots, forecast.acys_snapshot_rows TO grp_aixii_read';
    END IF;
END
$do$;
"""


def upgrade() -> None:
    op.execute(_CREATE_HEADER)
    op.execute("CREATE INDEX ix_acys_snapshots_created ON forecast.acys_snapshots (created_at DESC)")
    op.execute("CREATE INDEX ix_acys_snapshots_as_of ON forecast.acys_snapshots (as_of)")
    op.execute(_CREATE_ROWS)
    op.execute(_PKEY)
    op.execute(_GRANTS)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS forecast.acys_snapshot_rows")
    op.execute("DROP TABLE IF EXISTS forecast.acys_snapshots")
