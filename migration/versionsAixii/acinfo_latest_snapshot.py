"""aircraft_information: a tail's Cirium attributes from its NEWEST snapshot, not only the latest revision

MSN, Current Family, Owner and Manager in forecast.aircraft_information (and so in the fleet sheet
forecast.detailed_aircraft_information) came from the LATEST Cirium revision of each plan type only.
A tail that is in the forecast but no longer in that snapshot — a carry-forward / wet-lease tail,
or one Cirium dropped from the roster after it stopped flying for the operator (9H-FIT: last seen in
revision 116, flew 2024-2025) — got NULL in all four, although Cirium knows every one of them.

Now the CTE reads the newest snapshot of the tail in ANY revision. For a tail that IS in the latest
revision nothing changes: the latest revisions carry the highest ids, and the order was already
`revision_id DESC`. It is restricted to the tails the panel holds (the registration index makes that
~10 ms), so dropping the latest-revision join does not turn it into a scan of all Cirium history.

The sheet also stops showing an empty "Agreed Value / AW AVE / mUSD" for a year the tail did not fly:
the activity-weighted average has no activity to weight by, so it falls back to the time-weighted
"AVE" — which is what an activity weighting over a flat zero reduces to. Done in the sheet's source
view only; the matview keeps the NULL, so reports that read it are unchanged.

aircraft_information is a matview (owned by grp_aviation_write, refreshed by the panel): captured with
pg_get_viewdef and rebuilt with one anchored edit, indexes/owner/grants replayed, like
age_group_sort_facts. Its only dependents are the two sheet views, dropped and recreated around it.

Revision ID: acinfo_latest_snapshot
Revises: aircraft_info_edits
Create Date: 2026-09-24
"""
import importlib.util
import pathlib
import re

import sqlalchemy as sa
from alembic import op

_spec = importlib.util.spec_from_file_location(
    "_aircraft_info_edits", pathlib.Path(__file__).with_name("aircraft_info_edits.py"))
_edits = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_edits)

revision = "acinfo_latest_snapshot"
down_revision = "aircraft_info_edits"
branch_labels = None
depends_on = None

_AI = "forecast.aircraft_information"
_READ_ROLES = ("grp_aixii_read", "grp_aviation_write")

# pg_get_viewdef's rendering of the two CTE pieces that change (whitespace-tolerant)
_LATEST_REV_RE = re.compile(r"latest_rev AS \(.*?\),\s*cur_family AS \(", re.S)
_JOIN_RE = re.compile(r"FROM cirium\.ciriumaircrafts ca\s+JOIN latest_rev lr ON lr\.id = ca\.revision_id\s+")
_PANEL_REGS = ('WHERE ca."Registration" IN '
               '(SELECT DISTINCT "Registration" FROM forecast.acys_summary_grouped_by_reg) ')
_NEW_JOIN_RE = re.compile(r"FROM cirium\.ciriumaircrafts ca\s+WHERE .*?(?=ORDER BY)", re.S)
_LATEST_REV = """latest_rev AS (
    SELECT DISTINCT ON (plan_type) id FROM cirium.aircraftrevision
    ORDER BY plan_type, to_date(period, 'MM-YYYY') DESC, id DESC
), cur_family AS ("""

_AW = 'round((y."Activity-Weighted Average Agreed Value" / 1000000.0)::numeric, 2)'
_AW_NEW = ('round((coalesce(y."Activity-Weighted Average Agreed Value", '
           'y."Weighted Average Agreed Value") / 1000000.0)::numeric, 2)')


def _grant(obj: str) -> None:
    for role in _READ_ROLES:
        op.execute(f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN "
                   f"EXECUTE 'GRANT SELECT ON {obj} TO {role}'; END IF; END $$;")


def _capture(conn):
    body = conn.execute(sa.text(f"SELECT pg_get_viewdef('{_AI}'::regclass, true)")).scalar()
    body = body.strip().rstrip(";").strip()
    idx = [r[0] for r in conn.execute(sa.text(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = 'forecast' "
        "AND tablename = 'aircraft_information'"))]
    owner = conn.execute(sa.text(f"SELECT relowner::regrole::text FROM pg_class "
                                 f"WHERE oid = '{_AI}'::regclass")).scalar()
    return body, idx, owner


def _rebuild(new_ai_def: str, idx: list, owner: str, source_view: str) -> None:
    op.execute("DROP VIEW IF EXISTS forecast.detailed_aircraft_information")
    op.execute("DROP VIEW IF EXISTS forecast.detailed_aircraft_information_source")
    op.execute(f"DROP MATERIALIZED VIEW {_AI}")
    op.execute(f"CREATE MATERIALIZED VIEW {_AI} AS {new_ai_def}")
    for ix in idx:
        op.execute(ix)
    if owner:
        op.execute(f"ALTER MATERIALIZED VIEW {_AI} OWNER TO {owner}")
    _grant(_AI)
    op.execute(source_view)
    op.execute(_edits.VIEW)
    _grant("forecast.detailed_aircraft_information_source")
    _grant("forecast.detailed_aircraft_information")


def upgrade() -> None:
    body, idx, owner = _capture(op.get_bind())
    assert _LATEST_REV_RE.search(body) and _JOIN_RE.search(body), "aircraft_information CTE moved"
    new = _JOIN_RE.sub(lambda _m: "FROM cirium.ciriumaircrafts ca\n          " + _PANEL_REGS, body, 1)
    new = _LATEST_REV_RE.sub("cur_family AS (", new, 1)
    assert _AW in _edits.SOURCE_VIEW
    _rebuild(new, idx, owner, _edits.SOURCE_VIEW.replace(_AW, _AW_NEW, 1))


def downgrade() -> None:
    body, idx, owner = _capture(op.get_bind())
    assert _NEW_JOIN_RE.search(body), "aircraft_information CTE moved"
    old = _NEW_JOIN_RE.sub(lambda _m: "FROM cirium.ciriumaircrafts ca\n"
                                      "    JOIN latest_rev lr ON lr.id = ca.revision_id\n    ", body, 1)
    old = old.replace("cur_family AS (", _LATEST_REV, 1)
    _rebuild(old, idx, owner, _edits.SOURCE_VIEW)
