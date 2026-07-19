"""Convert the three forecast report VIEWs to MATERIALIZED VIEWs (refreshed after each panel run).

``forecast.acys_summary_grouped_by_reg``, ``forecast.aircraft_information`` and ``powerbi.z_dates_acys`` were
plain VIEWs — PowerBI recomputed each one on every query. ``forecast.acys_summary_grouped`` was already a
matview for exactly this reason; these three now join it, so the report reads four physical snapshots instead
of re-aggregating live. external-worker REFRESHes all four at the end of every forecast run (panel.py), in
dependency order, so the snapshots are never stale. (``forecast.acys_summary_by_day`` stays a TABLE, and
``powerbi.z_age_group`` stays a static VIEW — neither is converted.)

No SQL is duplicated: each view's live definition is captured with ``pg_get_viewdef`` and rematerialised, so
the matview body is byte-for-byte what the view computed. The source-of-truth chain builder
(``forecast_grouped_route_cols._rebuild``) was updated in lockstep (CREATE VIEW -> CREATE MATERIALIZED VIEW +
the index lists below), so a future full chain rebuild produces the same matviews.

Dependency order matters: ``aircraft_information`` reads ``acys_summary_grouped_by_reg``, so the view is
dropped first and the matview rebuilt second. Non-CONCURRENT REFRESH (brief ACCESS EXCLUSIVE, like the
existing grouped matview) — by_reg has no small natural unique key (its grain is the full GROUP BY), so no
unique index is added and CONCURRENTLY is not used.

Revision ID: forecast_chain_matviews
Revises: z_dates_data_type
Create Date: 2026-07-19
"""
import sqlalchemy as sa
from alembic import op

revision = "forecast_chain_matviews"
down_revision = "z_dates_data_type"
branch_labels = None
depends_on = None

# (object, its indexes) — kept identical to forecast_grouped_route_cols._{BY_REG,AIRCRAFT_INFO,Z_DATES}_INDEXES
_BY_REG = "forecast.acys_summary_grouped_by_reg"
_ACINFO = "forecast.aircraft_information"
_ZDATES = "powerbi.z_dates_acys"

_INDEXES = {
    _BY_REG: [
        'CREATE INDEX ix_by_reg_mkey     ON forecast.acys_summary_grouped_by_reg ("MERGED_KEY")',
        'CREATE INDEX ix_by_reg_reg      ON forecast.acys_summary_grouped_by_reg ("Registration")',
        'CREATE INDEX ix_by_reg_cy       ON forecast.acys_summary_grouped_by_reg ("Contract Year")',
        'CREATE INDEX ix_by_reg_dtype    ON forecast.acys_summary_grouped_by_reg ("Data Type")',
        'CREATE INDEX ix_by_reg_dateint  ON forecast.acys_summary_grouped_by_reg ("DateInt")',
    ],
    _ACINFO: [
        'CREATE INDEX ix_acinfo_mkey     ON forecast.aircraft_information ("MERGED_KEY")',
        'CREATE INDEX ix_acinfo_reg      ON forecast.aircraft_information ("Registration")',
        'CREATE INDEX ix_acinfo_agegroup ON forecast.aircraft_information ("Age Group")',
        'CREATE INDEX ix_acinfo_family   ON forecast.aircraft_information ("Current Family")',
    ],
    _ZDATES: [
        'CREATE INDEX ix_zdates_date  ON powerbi.z_dates_acys ("Date")',
        'CREATE INDEX ix_zdates_cy    ON powerbi.z_dates_acys ("Contract Year")',
        'CREATE INDEX ix_zdates_dtype ON powerbi.z_dates_acys ("Data Type")',
    ],
}

_OWNER = """
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aviation_write') THEN
    EXECUTE 'ALTER MATERIALIZED VIEW forecast.acys_summary_grouped_by_reg OWNER TO grp_aviation_write';
    EXECUTE 'ALTER MATERIALIZED VIEW forecast.aircraft_information        OWNER TO grp_aviation_write';
    EXECUTE 'ALTER MATERIALIZED VIEW powerbi.z_dates_acys                 OWNER TO grp_aviation_write';
  END IF;
END $$;
"""

_GRANTS = """
DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['grp_aixii_read','grp_aviation_write'] LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('GRANT SELECT ON forecast.acys_summary_grouped_by_reg, '
                     'forecast.aircraft_information, powerbi.z_dates_acys TO %I', r);
    END IF;
  END LOOP;
END $$;
"""


def _viewdef(conn, obj: str) -> str:
    body = conn.execute(sa.text("SELECT pg_get_viewdef(:o ::regclass, true)"), {"o": obj}).scalar()
    if not body:
        raise RuntimeError(f"could not read definition of {obj}")
    return body


def _convert(conn, drop_kw: str, create_kw: str, add_indexes: bool) -> None:
    # capture-then-drop in dependency order (aircraft_information depends on grouped_by_reg), then rebuild in
    # the reverse order so each object's dependency already exists in its new form.
    d_acinfo = _viewdef(conn, _ACINFO)
    op.execute(f"DROP {drop_kw} IF EXISTS {_ACINFO}")
    d_by_reg = _viewdef(conn, _BY_REG)
    op.execute(f"DROP {drop_kw} IF EXISTS {_BY_REG}")
    d_zdates = _viewdef(conn, _ZDATES)
    op.execute(f"DROP {drop_kw} IF EXISTS {_ZDATES}")

    for obj, body in ((_BY_REG, d_by_reg), (_ACINFO, d_acinfo), (_ZDATES, d_zdates)):
        op.execute(f"CREATE {create_kw} {obj} AS {body}")
        if add_indexes:
            for ix in _INDEXES[obj]:
                op.execute(ix)


def upgrade() -> None:
    conn = op.get_bind()
    _convert(conn, drop_kw="VIEW", create_kw="MATERIALIZED VIEW", add_indexes=True)
    op.execute(_OWNER)
    op.execute(_GRANTS)


def downgrade() -> None:
    conn = op.get_bind()
    # matview indexes vanish with the DROP MATERIALIZED VIEW; the plain views carry none.
    _convert(conn, drop_kw="MATERIALIZED VIEW", create_kw="VIEW", add_indexes=False)
    op.execute(_GRANTS)
