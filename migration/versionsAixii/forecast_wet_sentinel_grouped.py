"""A WET row carries the 0.00001 marker in every Agreed-Value column of acys_summary_grouped and by_reg.

external-worker's panel writes 0.00001 — not 0 — as the Agreed Value of a wet-lease month: a plain zero
reads as a real "$0 airframe" downstream. The four CY-shaped columns (inception / at end / weighted average /
activity-weighted) never saw it: they are computed from DRY months only, so a wet row showed either its
contract year's dry figures or, for a tail that was wet all year, four NULLs.

Now a wet row reads 0.00001 in all four, matching the "Agreed Value" it already carried — one marker,
consistently, in both matviews. acys_summary_grouped_by_reg needs no change of its own: it MAXes those
columns inside a row that is wet by definition, so it inherits the marker.

Nothing else moves. The dry rows' figures are untouched, and the model still computes the four from dry
months alone — this only decides what a WET row displays.

Rebuilding acys_summary_grouped means dropping everything that reads it. _drop_chain / _rebuild cover the
report chain but not the two slicer buckets, so those are captured (definition, indexes, owner) and put back
around the rebuild. Both shapes come from the source of truth _grouped(route_cols, wet_sentinel); downgrade
puts back the shape the chain revision built.

Revision ID: forecast_wet_sentinel_grouped
Revises: forecast_detail_type_cols
Create Date: 2026-09-09
"""
import os
import sys

import sqlalchemy as sa
from alembic import op

sys.path.insert(0, os.path.dirname(__file__))
from forecast_grouped_route_cols import _drop_chain, _rebuild  # noqa: E402

revision = "forecast_wet_sentinel_grouped"
down_revision = "forecast_detail_type_cols"
branch_labels = None
depends_on = None

# read acys_summary_grouped, so they must go before it and come back after — and they are NOT part of the
# chain builder, which is why this migration carries them itself.
_BUCKETS = ("forecast.acys_origin_bucket", "forecast.acys_destination_bucket")


def _capture(conn, obj: str):
    """Definition, indexes and owner of a matview, or None when it does not exist (a database built before
    the buckets were introduced)."""
    if conn.execute(sa.text("SELECT to_regclass(:o)"), {"o": obj}).scalar() is None:
        return None
    body = conn.execute(sa.text("SELECT pg_get_viewdef(:o ::regclass, true)"), {"o": obj}).scalar()
    schema, name = obj.split(".", 1)
    idx = [r[0] for r in conn.execute(
        sa.text("SELECT indexdef FROM pg_indexes WHERE schemaname = :s AND tablename = :t"),
        {"s": schema, "t": name})]
    owner = conn.execute(sa.text("SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = :o ::regclass"),
                         {"o": obj}).scalar()
    return body, idx, owner


def _restore(obj: str, captured) -> None:
    if captured is None:
        return
    body, idx, owner = captured
    op.execute(f"CREATE MATERIALIZED VIEW {obj} AS {body}")
    for ix in idx:
        op.execute(ix)
    op.execute(f'ALTER MATERIALIZED VIEW {obj} OWNER TO "{owner}"')
    op.execute(f"""
DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['grp_aixii_read','grp_aviation_write'] LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('GRANT SELECT ON {obj} TO %I', r);
    END IF;
  END LOOP;
END $$;
""")


def _swap(wet_sentinel: bool) -> None:
    conn = op.get_bind()
    captured = {obj: _capture(conn, obj) for obj in _BUCKETS}
    for obj in _BUCKETS:
        op.execute(f"DROP MATERIALIZED VIEW IF EXISTS {obj}")
    _drop_chain()
    _rebuild(route_cols=True, wet_sentinel=wet_sentinel)
    for obj in _BUCKETS:
        _restore(obj, captured[obj])


def upgrade() -> None:
    _swap(wet_sentinel=True)


def downgrade() -> None:
    _swap(wet_sentinel=False)
