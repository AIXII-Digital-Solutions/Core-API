"""Manual edits of the fleet sheet reach the forecast report: an overlay between the run and the rollup

The portal edits `forecast.detailed_aircraft_information` (overrides in forecast.aircraft_info_edits,
revision aircraft_info_edits). Until now an edit only changed what the sheet DISPLAYED; the report
PowerBI reads kept the model's figures. Now four of the edited fields are carried into the dataset
the report is built from, because they are the ones the dataset has per flight:

    Seats                        -> "Total Seats", and "Total PAX" (seats x the run's load factor)
    Agreed Value / INC / mUSD    -> "Agreed Value" of every month of that contract year
    Lease                        -> "Lease Type"
    Lease Type                   -> "Lease Dry Wet" (and with it the Wet sentinel on Agreed Value)

WHERE THE EDITS ARE APPLIED. Not in forecast.acys_summary_by_day and not in the snapshots — those stay
exactly what the model produced. They are applied by a VIEW over it,
`forecast.acys_summary_by_day_effective`, and the report chain (acys_summary_grouped and everything
above it) now reads that view instead of the table. So:

  * applying edits is a REFRESH of the report matviews, not a forecast run: nothing is fetched and
    nothing is forecast, because no edited field feeds the model's volumes or routes;
  * a snapshot restored later picks up the edits as they are THEN — the snapshot is never rewritten
    and never duplicated, and it can never carry an edit that has since been reverted;
  * the model's own figures are always still there to compare an edit against (the sheet's
    "Original Values" are read from the table, not from the edited rollup).

AGREED VALUE — ONE KNOB, THREE DERIVED FIGURES. The sheet's four value columns are all functions of the
MONTHLY agreed values of the contract year: INC is the first month's, EXP the last's, AVE their mean
and AW AVE the flight-weighted mean. An edit of INC therefore SCALES the whole year's monthly path by
INC_edit / INC_model, and the other three follow by construction (the depreciation shape is kept). Where
there is no model path to scale — no value at all, or a wet lease being turned dry — the year is flat at
the edited INC. AVE, AW AVE and EXP are no longer editable: they are consequences, not inputs.

EDITS CARRY INTO THE PROJECTED YEARS. An edit made on a contract year from the tail's last actual one
onwards also governs every LATER (forecast) year of that tail, up to the next year that has its own
edit — the projection starts from that year, so a corrected seat count or value is the one it should
continue from. An edit on an earlier, fully actual year governs that year alone: later actual years are
facts of their own.

KNOWING WHEN THE REPORT IS BEHIND. `forecast.acys_live_state` records which snapshot the staging table
holds and when the report was last refreshed (written by external-worker); a trigger stamps
`forecast.aircraft_info_edit_marks` per airline on every edit. The sheet's "Recalculation Pending" is
the comparison of the two.

The report chain is rebuilt with captured definitions (pg_get_viewdef + indexes + owner + grants), the
only change being where acys_summary_grouped reads from.

Revision ID: acys_edits_overlay
Revises: asg_matviews_owner
Create Date: 2026-09-24
"""
import importlib.util
import pathlib
import re

import sqlalchemy as sa
from alembic import op


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"_{name}", pathlib.Path(__file__).with_name(f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_edits = _load("aircraft_info_edits")

revision = "acys_edits_overlay"
down_revision = "asg_matviews_owner"
branch_labels = None
depends_on = None

_READ_ROLES = ("grp_aixii_read", "grp_aviation_write")

_TABLE = "forecast.acys_summary_by_day"
_EFFECTIVE = "forecast.acys_summary_by_day_effective"
_SHEET = "forecast.detailed_aircraft_information"
_SHEET_SOURCE = "forecast.detailed_aircraft_information_source"

# the report chain above the staging table, sources before dependents (z_dates_acys reads only dates
# and stays on the table)
_CHAIN = ["forecast.acys_summary_grouped",
          "forecast.acys_summary_grouped_by_reg",
          "forecast.acys_origin_bucket",
          "forecast.acys_destination_bucket",
          "forecast.acys_summary_grouped_by_reg_and_year",
          "forecast.aircraft_information"]

# sheet column -> (edits key, per-flight column)
_INC = "Agreed Value / INC / mUSD"
_DERIVED = ("Agreed Value / AVE / mUSD", "Agreed Value / AW AVE / mUSD", "Agreed Value / EXP / mUSD")
_CARRIED = ("Seats", _INC, "Lease", "Lease Type")

_OVERRIDDEN = {"Agreed Value", "Total Seats", "Total PAX", "Lease Type", "Lease Dry Wet"}

# Agreed Value is sentinelled at this for a wet lease (panel._merge_sql): not a price
_WET_AV = "0.00001"


def _grant(obj: str, roles=_READ_ROLES) -> None:
    for role in roles:
        op.execute(f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN "
                   f"EXECUTE 'GRANT SELECT ON {obj} TO {role}'; END IF; END $$;")


# ── state ─────────────────────────────────────────────────────────────────────────────────────

_STATE_DDL = [
    """CREATE TABLE forecast.acys_live_state (
        id           smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
        snapshot_id  bigint,
        refreshed_at timestamptz
    )""",
    "INSERT INTO forecast.acys_live_state (id) VALUES (1)",
    "COMMENT ON TABLE forecast.acys_live_state IS 'ONE row: which snapshot forecast.acys_summary_by_day "
    "holds (NULL while a run or restore is rewriting it) and when the report matviews were last "
    "refreshed. Written by external-worker.'",
    "ALTER TABLE forecast.acys_snapshots ADD COLUMN edits_applied_at timestamptz",
    """CREATE TABLE forecast.aircraft_info_edit_marks (
        airline    text PRIMARY KEY,
        changed_at timestamptz NOT NULL
    )""",
    "COMMENT ON TABLE forecast.aircraft_info_edit_marks IS 'When each airline''s fleet-sheet edits last "
    "changed (a revert included). Maintained by a trigger on forecast.aircraft_info_edits.'",
    """CREATE FUNCTION forecast.mark_aircraft_info_edit() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, forecast AS $fn$
    BEGIN
        INSERT INTO forecast.aircraft_info_edit_marks (airline, changed_at)
        VALUES (CASE WHEN TG_OP = 'DELETE' THEN OLD.airline ELSE NEW.airline END, now())
        ON CONFLICT (airline) DO UPDATE SET changed_at = EXCLUDED.changed_at;
        IF TG_OP = 'UPDATE' AND OLD.airline IS DISTINCT FROM NEW.airline THEN
            INSERT INTO forecast.aircraft_info_edit_marks (airline, changed_at) VALUES (OLD.airline, now())
            ON CONFLICT (airline) DO UPDATE SET changed_at = EXCLUDED.changed_at;
        END IF;
        RETURN NULL;
    END
    $fn$""",
    "CREATE TRIGGER aircraft_info_edit_marks AFTER INSERT OR UPDATE OR DELETE "
    "ON forecast.aircraft_info_edits FOR EACH ROW EXECUTE FUNCTION forecast.mark_aircraft_info_edit()",
]

# The model's own values of the carried fields for one sheet row, read from the TABLE (never the
# edited rollup) with the same rules the rollup uses. The sheet compares an edit against this, and so
# does the API before it stores one.
_RAW_FN = f"""
CREATE FUNCTION forecast.aircraft_info_raw(p_op text, p_reg text, p_cy text, p_dt text) RETURNS jsonb
LANGUAGE sql STABLE AS $fn$
    WITH r AS (
        SELECT to_date("Period", 'MM-YYYY') AS m, "Agreed Value" AS av, "Total Seats" AS seats,
               "Lease Type" AS lt, "Lease Dry Wet" AS ldw
        FROM {_TABLE}
        WHERE "Operator" = p_op AND "Registration" = p_reg AND "Contract Year" = p_cy AND "Data Type" = p_dt
    ),
    mon AS (SELECT m, max(av) AS av, bool_and(ldw IS DISTINCT FROM 'Wet') AS dry FROM r GROUP BY m),
    last_m AS (SELECT max(m) AS m FROM r)
    SELECT jsonb_build_object(
        'Seats', (SELECT max(seats) FROM r),
        '{_INC}', (SELECT round(((array_agg(av ORDER BY m) FILTER (WHERE dry AND av > 0))[1]
                                 / 1000000.0)::numeric, 2) FROM mon),
        'Lease', (SELECT coalesce(nullif(max(lt), ''), 'Not Leased') FROM r WHERE m = (SELECT m FROM last_m)),
        'Lease Type', (SELECT coalesce(nullif(max(ldw), ''), 'Not Leased') FROM r
                       WHERE m = (SELECT m FROM last_m)))
$fn$
"""


def _gov(field: str) -> str:
    """The contract year whose edit governs `field` for cell c: its own, else — for a year after the
    tail's last actual one — the latest edited year from that actual year on."""
    return f"""(SELECT e.contract_year FROM forecast.aircraft_info_edits e
             WHERE e.airline = c.op AND e.registration = c.reg AND (e.edits -> '{field}') IS NOT NULL
               AND (e.contract_year = c.cy
                    OR (c.cy > a.anchor_cy AND e.contract_year >= a.anchor_cy AND e.contract_year < c.cy))
             ORDER BY e.contract_year DESC LIMIT 1)"""


def _effective_view(cols: list[str]) -> str:
    def out(col: str) -> str:
        if col == "Agreed Value":
            return f"""CASE WHEN x.op IS NULL THEN b."Agreed Value"
         WHEN (CASE WHEN x.has_ldw THEN x.ldw ELSE b."Lease Dry Wet" END) = 'Wet' THEN {_WET_AV}
         WHEN x.has_inc THEN
              CASE WHEN x.inc_usd IS NULL THEN NULL
                   WHEN b."Lease Dry Wet" = 'Wet' OR coalesce(b."Agreed Value", 0) <= {_WET_AV}
                        OR coalesce(x.inc_raw, 0) <= 0 THEN x.inc_usd
                   ELSE b."Agreed Value" * (x.inc_usd / x.inc_raw) END
         ELSE b."Agreed Value" END AS "Agreed Value\""""
        if col == "Total Seats":
            return 'CASE WHEN x.has_seats THEN x.seats ELSE b."Total Seats" END AS "Total Seats"'
        if col == "Total PAX":
            return ('CASE WHEN x.has_seats THEN x.seats * coalesce(b."Total PAX" / nullif(b."Total Seats", 0), '
                    'f.factor, 0.8) ELSE b."Total PAX" END AS "Total PAX"')
        if col == "Lease Type":
            return 'CASE WHEN x.has_lease THEN x.lease ELSE b."Lease Type" END AS "Lease Type"'
        if col == "Lease Dry Wet":
            return 'CASE WHEN x.has_ldw THEN x.ldw ELSE b."Lease Dry Wet" END AS "Lease Dry Wet"'
        return f'b."{col}"'

    select = ",\n    ".join(out(c) for c in cols)
    return f"""
CREATE VIEW {_EFFECTIVE} AS
WITH ed AS (      -- the tails that have any edit at all: everything below is scoped to them
    SELECT DISTINCT airline, registration FROM forecast.aircraft_info_edits
),
cells AS (        -- every (operator, tail, contract year) of an edited tail in the run
    SELECT b."Operator" AS op, b."Registration" AS reg, b."Contract Year" AS cy,
           bool_or(b."Data Type" = 'Actuals') AS has_act
    FROM {_TABLE} b
    JOIN ed ON ed.airline = b."Operator" AND ed.registration = b."Registration"
    WHERE b."Contract Year" IS NOT NULL
    GROUP BY 1, 2, 3
),
anchor AS (       -- the tail's last contract year that has facts; later years are projection only
    SELECT op, reg, max(cy) FILTER (WHERE has_act) AS anchor_cy FROM cells GROUP BY 1, 2
),
gov AS (
    SELECT c.op, c.reg, c.cy,
           {_gov("Seats")} AS seats_from,
           {_gov(_INC)} AS inc_from,
           {_gov("Lease")} AS lease_from,
           {_gov("Lease Type")} AS ldw_from
    FROM cells c JOIN anchor a ON a.op = c.op AND a.reg = c.reg
),
inc_mon AS (      -- the model's monthly values of every year that carries an INC edit
    SELECT b."Operator" AS op, b."Registration" AS reg, b."Contract Year" AS cy, b."Data Type" AS dt,
           to_date(b."Period", 'MM-YYYY') AS m, max(b."Agreed Value") AS av,
           bool_and(b."Lease Dry Wet" IS DISTINCT FROM 'Wet') AS dry
    FROM {_TABLE} b
    JOIN forecast.aircraft_info_edits e
      ON e.airline = b."Operator" AND e.registration = b."Registration" AND e.contract_year = b."Contract Year"
     AND (e.edits -> '{_INC}') IS NOT NULL
    GROUP BY 1, 2, 3, 4, 5
),
inc_raw AS (      -- ... and its model INC, the facts half first (as the sheet shows it)
    SELECT DISTINCT ON (op, reg, cy) op, reg, cy, inc
    FROM (SELECT op, reg, cy, dt, (array_agg(av ORDER BY m) FILTER (WHERE dry AND av > 0))[1] AS inc
          FROM inc_mon GROUP BY 1, 2, 3, 4) t
    ORDER BY op, reg, cy, (dt = 'Actuals') DESC, dt
),
eff AS (          -- per cell, the governing value of each carried field
    SELECT g.op, g.reg, g.cy,
           g.seats_from IS NOT NULL AS has_seats, (es.edits ->> 'Seats')::int AS seats,
           g.inc_from IS NOT NULL AS has_inc,
           ((ei.edits ->> '{_INC}')::numeric * 1000000)::double precision AS inc_usd, ir.inc AS inc_raw,
           g.lease_from IS NOT NULL AS has_lease, el.edits ->> 'Lease' AS lease,
           g.ldw_from IS NOT NULL AS has_ldw, ew.edits ->> 'Lease Type' AS ldw
    FROM gov g
    LEFT JOIN forecast.aircraft_info_edits es
           ON es.airline = g.op AND es.registration = g.reg AND es.contract_year = g.seats_from
    LEFT JOIN forecast.aircraft_info_edits ei
           ON ei.airline = g.op AND ei.registration = g.reg AND ei.contract_year = g.inc_from
    LEFT JOIN inc_raw ir ON ir.op = g.op AND ir.reg = g.reg AND ir.cy = g.inc_from
    LEFT JOIN forecast.aircraft_info_edits el
           ON el.airline = g.op AND el.registration = g.reg AND el.contract_year = g.lease_from
    LEFT JOIN forecast.aircraft_info_edits ew
           ON ew.airline = g.op AND ew.registration = g.reg AND ew.contract_year = g.ldw_from
    WHERE g.seats_from IS NOT NULL OR g.inc_from IS NOT NULL
       OR g.lease_from IS NOT NULL OR g.ldw_from IS NOT NULL
),
fac AS (          -- the run's PAX load factor, for a tail whose own rows carry no seats to derive it from
    SELECT o.op, (SELECT b."Total PAX" / b."Total Seats" FROM {_TABLE} b
                  WHERE b."Operator" = o.op AND b."Total Seats" > 0 AND b."Total PAX" IS NOT NULL
                  LIMIT 1) AS factor
    FROM (SELECT DISTINCT op FROM eff WHERE has_seats) o
)
SELECT
    {select}
FROM {_TABLE} b
LEFT JOIN eff x ON x.op = b."Operator" AND x.reg = b."Registration" AND x.cy = b."Contract Year"
LEFT JOIN fac f ON f.op = x.op
"""


# ── the sheet ─────────────────────────────────────────────────────────────────────────────────

def _sheet_view() -> str:
    cols = []
    for name, cast in _edits._COLUMNS:
        if cast is None:
            cols.append(f'    s."{name}"')
        elif name == _INC:
            cols.append(f"""    CASE WHEN (e.edits -> '{_INC}') IS NOT NULL THEN round((e.edits ->> '{_INC}')::numeric, 2)
         ELSE s."{_INC}" END AS "{_INC}\"""")
        elif name in _DERIVED:
            # consequences of INC: shown scaled at once, before the report is recalculated (after it,
            # the source already carries the edit and the factor is 1)
            cols.append(f"""    CASE WHEN (e.edits -> '{_INC}') IS NULL THEN s."{name}"
         WHEN (e.edits ->> '{_INC}') IS NULL THEN NULL
         WHEN coalesce(s."{_INC}", 0) <= 0 THEN round((e.edits ->> '{_INC}')::numeric, 2)
         WHEN s."{name}" IS NULL THEN NULL
         ELSE round(s."{name}" * (e.edits ->> '{_INC}')::numeric / s."{_INC}", 2) END AS "{name}\"""")
        else:
            cols.append(_edits._col(name, cast))
    carried_any = " OR ".join(f"(e2.edits -> '{k}') IS NOT NULL" for k in _CARRIED)
    return f"""
CREATE VIEW {_SHEET} AS
SELECT
    s.id,
    s."Airline",
{(',' + chr(10)).join(cols)},
    d.fields IS NOT NULL                        AS "Edited",
    coalesce(d.fields, '{{}}'::text[])          AS "Edited Fields",
    d.originals                                 AS "Original Values",
    CASE WHEN d.fields IS NOT NULL THEN e.updated_at END AS "Edited At",
    CASE WHEN d.fields IS NOT NULL THEN e.updated_by END AS "Edited By",
    inh.cy                                      AS "Edits Inherited From",
    coalesce(mk.changed_at > ls.refreshed_at, mk.changed_at IS NOT NULL) AS "Recalculation Pending"
FROM (
    SELECT src.*,
           max(src."Contract Year") FILTER (WHERE src."Data Type" = 'Actuals')
               OVER (PARTITION BY src."Airline", src."Registration") AS _anchor_cy
    FROM {_SHEET_SOURCE} src
) s
LEFT JOIN forecast.aircraft_info_edits e
       ON e.airline = s."Airline"
      AND e.registration = s."Registration"
      AND e.contract_year = s."Contract Year"
LEFT JOIN LATERAL (
    -- the model's values of the carried fields (the rollup already holds the edit once recalculated)
    SELECT forecast.aircraft_info_raw(s."Airline", s."Registration", s."Contract Year", s."Data Type") AS raw
    WHERE e.id IS NOT NULL
) rw ON true
LEFT JOIN LATERAL (
    SELECT array_agg(j.key ORDER BY j.key)                                   AS fields,
           jsonb_object_agg(j.key, (to_jsonb(s) || coalesce(rw.raw, '{{}}'::jsonb)) -> j.key) AS originals
    FROM jsonb_each(e.edits) j
    WHERE j.value IS DISTINCT FROM ((to_jsonb(s) || coalesce(rw.raw, '{{}}'::jsonb)) -> j.key)
) d ON true
LEFT JOIN LATERAL (
    -- a projected year takes its carried fields from the latest edited year since the last actual one
    SELECT e2.contract_year AS cy FROM forecast.aircraft_info_edits e2
    WHERE e2.airline = s."Airline" AND e2.registration = s."Registration"
      AND s."Contract Year" > s._anchor_cy
      AND e2.contract_year >= s._anchor_cy AND e2.contract_year < s."Contract Year"
      AND ({carried_any})
    ORDER BY e2.contract_year DESC LIMIT 1
) inh ON true
LEFT JOIN forecast.aircraft_info_edit_marks mk ON mk.airline = s."Airline"
LEFT JOIN forecast.acys_live_state ls ON ls.id = 1
"""


# ── the chain ────────────────────────────────────────────────────────────────────────────────

def _capture(conn, obj: str) -> dict:
    sch, name = obj.split(".")
    body = conn.execute(sa.text(f"SELECT pg_get_viewdef('{obj}'::regclass, true)")).scalar()
    idx = [r[0] for r in conn.execute(sa.text(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = :s AND tablename = :n"), {"s": sch, "n": name})]
    owner = conn.execute(sa.text(f"SELECT relowner::regrole::text FROM pg_class WHERE oid = '{obj}'::regclass")).scalar()
    grants = [(r[0], r[1]) for r in conn.execute(sa.text(
        f"SELECT a.grantee::regrole::text, a.privilege_type FROM pg_class c, aclexplode(c.relacl) a "
        f"WHERE c.oid = '{obj}'::regclass AND a.grantee <> c.relowner AND a.grantee <> 0"))]
    return {"def": body.strip().rstrip(";").strip(), "idx": idx, "owner": owner, "grants": grants}


_FROM_ALIASED = re.compile(r"FROM forecast\.acys_summary_by_day s\b")
_FROM_BARE = re.compile(r"FROM forecast\.acys_summary_by_day\b(?!_)")
_FROM_EFF_ALIASED = re.compile(r"FROM forecast\.acys_summary_by_day_effective s\b")
_FROM_EFF_BARE = re.compile(r"FROM forecast\.acys_summary_by_day_effective acys_summary_by_day\b")


def _to_effective(body: str) -> str:
    body = _FROM_ALIASED.sub(f"FROM {_EFFECTIVE} s", body)
    return _FROM_BARE.sub(f"FROM {_EFFECTIVE} acys_summary_by_day", body)


def _to_table(body: str) -> str:
    body = _FROM_EFF_ALIASED.sub(f"FROM {_TABLE} s", body)
    return _FROM_EFF_BARE.sub(f"FROM {_TABLE}", body)


def _rebuild_chain(conn, rewrite, before_create=None) -> None:
    meta = {obj: _capture(conn, obj) for obj in _CHAIN}
    op.execute(f"DROP VIEW IF EXISTS {_SHEET}")
    op.execute(f"DROP VIEW IF EXISTS {_SHEET_SOURCE}")
    for obj in reversed(_CHAIN):
        op.execute(f"DROP MATERIALIZED VIEW {obj}")
    if before_create:
        before_create()
    for obj in _CHAIN:
        m = meta[obj]
        body = rewrite(m["def"]) if obj == _CHAIN[0] else m["def"]
        op.execute(f"CREATE MATERIALIZED VIEW {obj} AS {body}")
        for ix in m["idx"]:
            op.execute(ix)
        if m["owner"]:
            op.execute(f"ALTER MATERIALIZED VIEW {obj} OWNER TO {m['owner']}")
        for grantee, priv in m["grants"]:
            op.execute(f'GRANT {priv} ON {obj} TO "{grantee}"')
    return meta


def upgrade() -> None:
    conn = op.get_bind()
    for stmt in _STATE_DDL:
        op.execute(stmt)
    op.execute(_RAW_FN)

    cols = [r[0] for r in conn.execute(sa.text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'forecast' AND table_name = 'acys_summary_by_day' ORDER BY ordinal_position"))]
    assert _OVERRIDDEN <= set(cols), f"acys_summary_by_day lost a column: {_OVERRIDDEN - set(cols)}"
    op.execute(_effective_view(cols))
    _grant(_EFFECTIVE)

    grouped = _capture(conn, _CHAIN[0])["def"]
    assert _FROM_ALIASED.search(grouped) and _FROM_BARE.search(_FROM_ALIASED.sub("", grouped)), \
        "acys_summary_grouped no longer reads acys_summary_by_day the way this migration expects"
    _rebuild_chain(conn, _to_effective)

    op.execute(_edits.SOURCE_VIEW)
    op.execute(_sheet_view())
    _grant(_SHEET_SOURCE)
    _grant(_SHEET)
    _grant("forecast.acys_live_state")
    _grant("forecast.aircraft_info_edit_marks")


def downgrade() -> None:
    conn = op.get_bind()
    _rebuild_chain(conn, _to_table)
    op.execute(_edits.SOURCE_VIEW)
    op.execute(_edits.VIEW)
    _grant(_SHEET_SOURCE)
    _grant(_SHEET)
    op.execute(f"DROP VIEW {_EFFECTIVE}")
    op.execute("DROP FUNCTION forecast.aircraft_info_raw(text, text, text, text)")
    op.execute("DROP TRIGGER aircraft_info_edit_marks ON forecast.aircraft_info_edits")
    op.execute("DROP FUNCTION forecast.mark_aircraft_info_edit()")
    op.execute("DROP TABLE forecast.aircraft_info_edit_marks")
    op.execute("ALTER TABLE forecast.acys_snapshots DROP COLUMN edits_applied_at")
    op.execute("DROP TABLE forecast.acys_live_state")
