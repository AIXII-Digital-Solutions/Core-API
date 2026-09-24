"""powerbi.last_seen_fleet: a "Stationary for more than 24h" flag beside the 2h one

The report already tells a tail FR24 has not reported for two hours; it now also tells one that has
been silent for a full day, by the same rule on the same raw timestamp — only the window differs.

The view text is the previous revision's, loaded by file path (alembic imports a revision as a
standalone module), with one anchored edit — the new output column after the 2h one — asserted.

Revision ID: pbi_fleet_stationary_24h
Revises: pbi_fleet_logo_url
Create Date: 2026-09-23
"""
import importlib.util
import pathlib

from alembic import op

_spec = importlib.util.spec_from_file_location(
    "_pbi_fleet_logo_url", pathlib.Path(__file__).with_name("pbi_fleet_logo_url.py"))
_prev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_prev)
_PREV_VIEW = _prev.VIEW

revision = "pbi_fleet_stationary_24h"
down_revision = "pbi_fleet_logo_url"
branch_labels = None
depends_on = None

_READ_ROLES = _prev._READ_ROLES
_VIEW_COMMENT = _prev._VIEW_COMMENT

_STATIONARY_LONG_HOURS = 24

_OUT_2H = """(last_seen < now() - interval '2 hours') AS "Stationary for more than 2h"\n"""
_OUT_2H_NEW = (_OUT_2H.rstrip("\n") + ",\n"
               f"    (last_seen < now() - interval '{_STATIONARY_LONG_HOURS} hours')"
               f""" AS "Stationary for more than {_STATIONARY_LONG_HOURS}h"\n""")

VIEW = _PREV_VIEW.replace(_OUT_2H, _OUT_2H_NEW, 1)


def upgrade() -> None:
    assert _PREV_VIEW.count(_OUT_2H) == 1, "the previous view changed shape around the 2h column"
    assert VIEW.count('"Stationary for more than 24h"') == 1

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
