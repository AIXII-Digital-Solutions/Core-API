"""forecast.acys_origin_bucket / acys_destination_bucket: VIEW -> MATERIALIZED VIEW, sourced from
forecast.acys_summary_grouped instead of forecast.acys_summary_by_day.

These two single-column PowerBI slicer lookups listed every distinct 'City (Country)' and standalone 'Country'
of each side (+ a trailing 'Others'). They read the per-request TABLE acys_summary_by_day, so they went EMPTY
whenever a run TRUNCATEd it mid-flight (the table is cleared at panel step 2 and refilled at merge) — while the
report's other objects, being matviews, kept serving the last good snapshot. Reading from acys_summary_grouped
(a matview holding that same stable snapshot) fixes the mismatch: the buckets now track exactly what the report
shows. It is also cheaper — grouped is a physical route-level rollup (far fewer rows than the per-flight
by_day), and DISTINCT City/Country over it is a small scan.

Made MATERIALIZED so PowerBI reads a stored ~few-hundred-row list instead of a DISTINCT scan on every slicer
render; external-worker REFRESHes both right after it refreshes grouped (panel.py), so they never go stale.
Owned by grp_aviation_write (only an owner may REFRESH). A UNIQUE index on the single column (the values are
distinct by construction — UNION dedups, all non-null) both serves the slicer and keeps a future CONCURRENT
refresh possible.

The historical migration forecast_location_value_views (which first created them as views over by_day) is left
as-is; this is the forward migration that supersedes it.

Revision ID: bucket_matviews_from_grouped
Revises: forecast_chain_matviews
Create Date: 2026-07-19
"""
from alembic import op

revision = "bucket_matviews_from_grouped"
down_revision = "forecast_chain_matviews"
branch_labels = None
depends_on = None

_SRC_MV = "forecast.acys_summary_grouped"       # NEW source (stable snapshot)
_SRC_TBL = "forecast.acys_summary_by_day"        # OLD source (per-request table) — for downgrade


def _body(src: str, col: str, city: str, country: str) -> str:
    """The DISTINCT-bucket SELECT, parameterised by source relation — identical shape to
    forecast_location_value_views._view, only the FROM changes."""
    return f"""
SELECT "{col}" FROM (
    SELECT DISTINCT nullif("{city}",'') || ' (' || nullif("{country}",'') || ')' AS "{col}"
    FROM {src}
    WHERE nullif("{city}",'') IS NOT NULL AND nullif("{country}",'') IS NOT NULL
    UNION
    SELECT DISTINCT nullif("{country}",'')
    FROM {src}
    WHERE nullif("{country}",'') IS NOT NULL
    UNION
    SELECT 'Others'
) t
ORDER BY ("{col}" = 'Others'), "{col}"
"""


_BUCKETS = [
    # (name, column, city col, country col, unique-index name)
    ("acys_origin_bucket", "Origin Bucket", "Origin City", "Origin Country", "uq_acys_origin_bucket"),
    ("acys_destination_bucket", "Destination Bucket", "Destination City", "Destination Country",
     "uq_acys_destination_bucket"),
]

_GRANTS = """
DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['grp_aixii_read','grp_aviation_write'] LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('GRANT SELECT ON forecast.acys_origin_bucket, forecast.acys_destination_bucket TO %I', r);
    END IF;
  END LOOP;
END $$;
"""


def _owner() -> None:
    op.execute("""
    DO $$
    BEGIN
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aviation_write') THEN
        EXECUTE 'ALTER MATERIALIZED VIEW forecast.acys_origin_bucket      OWNER TO grp_aviation_write';
        EXECUTE 'ALTER MATERIALIZED VIEW forecast.acys_destination_bucket OWNER TO grp_aviation_write';
      END IF;
    END $$;
    """)


def upgrade() -> None:
    for name, col, city, country, uq in _BUCKETS:
        op.execute(f"DROP VIEW IF EXISTS forecast.{name}")
        op.execute(f"CREATE MATERIALIZED VIEW forecast.{name} AS {_body(_SRC_MV, col, city, country)}")
        op.execute(f'CREATE UNIQUE INDEX {uq} ON forecast.{name} ("{col}")')
    _owner()
    op.execute(_GRANTS)


def downgrade() -> None:
    for name, col, city, country, _uq in _BUCKETS:
        op.execute(f"DROP MATERIALIZED VIEW IF EXISTS forecast.{name}")
        op.execute(f"CREATE VIEW forecast.{name} AS {_body(_SRC_TBL, col, city, country)}")
    op.execute(_GRANTS)
