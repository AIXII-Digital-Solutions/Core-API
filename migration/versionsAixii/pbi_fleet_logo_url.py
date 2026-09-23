"""powerbi.last_seen_fleet carries the operator's logo URL

The fleet map labels every dot with an airline name and had nothing to draw beside it. The logo
already has a home — `ref.airline.logo_url`, a URL into the platform image store, never the bytes —
so the report only needs it resolved per row, the way the airline name itself already is.

WHERE THE MATCH COMES FROM. A row's `Airline Name` is one of two things: the name the cirium
matview resolved, which is `ref.airline.airline_name` VERBATIM (that is the column its LATERAL
selects), or the name typed by hand next to a registration in `api.registration.airline` for a tail
no reference name matched. The first is an equality; the second is whatever somebody typed, so it is
compared case- and whitespace-insensitively. A name that is in neither is simply a row with no logo,
which is also what an airline whose `logo_url` is still NULL looks like — the report has one empty
case to render, not two.

The lookup is a LATERAL with LIMIT 1 rather than a plain join: `ref.airline.airline_name` carries an
index, not a unique constraint, and a duplicated name must not multiply the fleet rows. Of two rows
with the same name the one that HAS a logo wins.

THE COLUMN IS EMPTY UNTIL THE URLS ARE FILLED. All 22 airlines have `logo_url` NULL today; they are
filled through `PATCH /ref/airlines/{id}` (or by hand), and the view picks them up with no rebuild.

The view text is the previous revision's, loaded by file path (alembic imports a revision as a
standalone module), with three anchored edits — the lookup, the column it carries through `base`,
and the output column — each asserted.

Revision ID: pbi_fleet_logo_url
Revises: audit_index_tuning
Create Date: 2026-09-23
"""
import importlib.util
import pathlib

from alembic import op

_spec = importlib.util.spec_from_file_location(
    "_pbi_ground_snap", pathlib.Path(__file__).with_name("pbi_ground_snap.py"))
_prev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_prev)
_PREV_VIEW = _prev.VIEW

revision = "pbi_fleet_logo_url"
down_revision = "audit_index_tuning"
branch_labels = None
depends_on = None

# what the schema's objects grant; the default privileges set by powerbi_read_grants already cover a
# rebuild, and this repeats it so the report cannot lose its grant if that default is ever dropped
_READ_ROLES = "grp_aixii_read, grp_aviation_write"

_VIEW_COMMENT = (
    "'The insured fleet with each tail''s last known position for the PowerBI map. A grounded "
    "aircraft is drawn at its airport inside a 100 m circle, with speed 0.'")

# 1. carry the logo through the base CTE
_BASE_COLS = "           f.asg_other, f.operator, f.master_series, f.sub_series,\n"
_BASE_COLS_NEW = _BASE_COLS + "           logo.logo_url,\n"

# 2. resolve it from the same name the row shows. `manual` and `f` are both already in the FROM, so
#    the LATERAL sees the resolved name without repeating the join.
_POS_JOIN = "    JOIN pos p ON p.reg_key = f.reg_key\n"
_LOGO_JOIN = _POS_JOIN + """    -- the operator's logo, by the name this row resolved to: the matview's match is
    -- ref.airline.airline_name verbatim, the hand-typed fallback is whatever somebody wrote
    LEFT JOIN LATERAL (
        SELECT al.logo_url
        FROM ref.airline al
        WHERE lower(btrim(al.airline_name)) = lower(btrim(coalesce(f.airline_name, manual.airline)))
        ORDER BY (al.logo_url IS NULL), al.id
        LIMIT 1
    ) logo ON true
"""

# 3. the output column, beside the name it belongs to
_OUT_NAME = '    airline_name                                      AS "Airline Name",\n'
_OUT_NAME_NEW = _OUT_NAME + '    logo_url                                          AS "logo_url",\n'

VIEW = (_PREV_VIEW
        .replace(_BASE_COLS, _BASE_COLS_NEW, 1)
        .replace(_POS_JOIN, _LOGO_JOIN, 1)
        .replace(_OUT_NAME, _OUT_NAME_NEW, 1))


def upgrade() -> None:
    for anchor in (_BASE_COLS, _POS_JOIN, _OUT_NAME):
        assert _PREV_VIEW.count(anchor) == 1, "the previous view changed shape around " + anchor.strip()
    assert VIEW.count("ref.airline al") == 1 and VIEW.count('AS "logo_url"') == 1

    op.execute("DROP VIEW IF EXISTS powerbi.last_seen_fleet")
    op.execute(VIEW)
    # a DROP takes the comment and the ACL with it
    op.execute(f"COMMENT ON VIEW powerbi.last_seen_fleet IS {_VIEW_COMMENT}")
    op.execute(f"GRANT SELECT ON powerbi.last_seen_fleet TO {_READ_ROLES}")


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS powerbi.last_seen_fleet")
    op.execute(_PREV_VIEW)
    op.execute(f"COMMENT ON VIEW powerbi.last_seen_fleet IS {_VIEW_COMMENT}")
    op.execute(f"GRANT SELECT ON powerbi.last_seen_fleet TO {_READ_ROLES}")
