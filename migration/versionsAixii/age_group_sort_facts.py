"""Add "Age Group Sort" (numeric 1..10) to every FACT matview that carries "Age Group", so PowerBI can sort
the bands there too (not only on the powerbi.z_age_group dimension).

Objects that carry "Age Group": forecast.acys_summary_grouped, forecast.acys_summary_grouped_by_reg,
forecast.aircraft_information (all matviews). "Age Group" is text like "1. …" .. "10. …", which PowerBI sorts
as TEXT ("10. …" lands between "1." and "2."). So each gets a companion numeric "Age Group Sort" =
split_part("Age Group", '.', 1)::int; in PowerBI set Sort "Age Group" BY "Age Group Sort" on each. (Same trick
as z_age_group."Age Group Sort" / z_dates_acys."MonthSortInContractYear".)

A matview cannot ALTER ADD COLUMN, so each must be dropped and recreated. This migration does NOT duplicate the
(large, helper-function-built) source SQL: it CAPTURES each object's live definition (pg_get_viewdef), indexes
(pg_get_indexdef) and owner, then rebuilds the object as `SELECT sub.*, <sort> FROM (<captured def>) sub`, and
replays the captured indexes + owner + the standard read grants. acys_summary_grouped has two matview
dependents that don't carry "Age Group" (acys_origin_bucket, acys_destination_bucket) — they are dropped and
recreated unchanged (from their captured def) so grouped can be dropped. Rebuild happens in dependency order.
by_day is populated, so the recreated grouped repopulates WITH DATA; the panel refreshes all of them each run
anyway.

The source of truth forecast_grouped_route_cols (_grouped / _BY_REG / _AIRCRAFT_INFO) was updated to emit the
column natively, and its _drop_chain to drop the buckets first, so a future full chain rebuild stays correct.

Revision ID: age_group_sort_facts
Revises: z_age_group_sort
Create Date: 2026-07-20
"""
import sqlalchemy as sa
from alembic import op

revision = "age_group_sort_facts"
down_revision = "z_age_group_sort"
branch_labels = None
depends_on = None

_G = "forecast.acys_summary_grouped"
_BR = "forecast.acys_summary_grouped_by_reg"
_AI = "forecast.aircraft_information"
_OB = "forecast.acys_origin_bucket"
_DB = "forecast.acys_destination_bucket"

_AFFECTED = {_G, _BR, _AI}                 # get "Age Group Sort" added/removed
_DROP_ORDER = [_AI, _DB, _OB, _BR, _G]      # dependents before their sources
_CREATE_ORDER = [_G, _BR, _AI, _OB, _DB]    # sources before dependents

_SORT_EXPR = 'split_part(sub."Age Group", \'.\', 1)::int AS "Age Group Sort"'
_READ_ROLES = ("grp_aixii_read", "grp_aviation_write")


def _split(obj):
    s, n = obj.split(".")
    return s, n


def _capture(conn, obj):
    sch, name = _split(obj)
    body = conn.execute(sa.text("SELECT pg_get_viewdef(:o ::regclass, true)"), {"o": obj}).scalar()
    # pg_get_viewdef ends the query with ';' — strip it, else wrapping as `FROM (<def>;) sub` is a syntax error.
    body = body.strip().rstrip(";").strip()
    idx = [r[0] for r in conn.execute(sa.text(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = :s AND tablename = :n"), {"s": sch, "n": name})]
    owner = conn.execute(sa.text(
        "SELECT r.rolname FROM pg_class c JOIN pg_namespace ns ON ns.oid = c.relnamespace "
        "JOIN pg_roles r ON r.oid = c.relowner WHERE ns.nspname = :s AND c.relname = :n"),
        {"s": sch, "n": name}).scalar()
    cols = [r[0] for r in conn.execute(sa.text(
        "SELECT a.attname FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
        "JOIN pg_namespace ns ON ns.oid = c.relnamespace "
        "WHERE ns.nspname = :s AND c.relname = :n AND a.attnum > 0 AND NOT a.attisdropped "
        "ORDER BY a.attnum"), {"s": sch, "n": name})]
    return {"def": body, "idx": idx, "owner": owner, "cols": cols}


def _grant(obj):
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


def _run(add_sort: bool) -> None:
    conn = op.get_bind()
    meta = {obj: _capture(conn, obj) for obj in _CREATE_ORDER}
    for obj in _DROP_ORDER:
        op.execute(f"DROP MATERIALIZED VIEW IF EXISTS {obj}")
    for obj in _CREATE_ORDER:
        m = meta[obj]
        if obj in _AFFECTED:
            if add_sort:
                body = f'SELECT sub.*, {_SORT_EXPR} FROM ({m["def"]}) sub'
            else:
                keep = ", ".join(f'sub."{c}"' for c in m["cols"] if c != "Age Group Sort")
                body = f'SELECT {keep} FROM ({m["def"]}) sub'
        else:
            body = m["def"]
        op.execute(f"CREATE MATERIALIZED VIEW {obj} AS {body}")
        for ix in m["idx"]:
            op.execute(ix)
        if m["owner"]:
            op.execute(f'ALTER MATERIALIZED VIEW {obj} OWNER TO {m["owner"]}')
        _grant(obj)


def upgrade() -> None:
    _run(add_sort=True)


def downgrade() -> None:
    _run(add_sort=False)
