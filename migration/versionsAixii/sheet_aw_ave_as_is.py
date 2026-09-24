"""Fleet sheet: "Agreed Value / AW AVE / mUSD" shows the matview's value again, with no AVE fallback

acinfo_latest_snapshot made the sheet's source view fall back to the time-weighted AVE when the
activity-weighted average was NULL (a year without flights). That is wrong: AW AVE is weighted by
activity, and a year with no activity has no such figure — substituting AVE presents a number the
model never computed. The column is the matview's value again, NULL included.

Only the two sheet views are rebuilt; aircraft_information keeps its newest-snapshot attributes.

Revision ID: sheet_aw_ave_as_is
Revises: acinfo_latest_snapshot
Create Date: 2026-09-24
"""
import importlib.util
import pathlib

from alembic import op


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"_{name}", pathlib.Path(__file__).with_name(f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_edits = _load("aircraft_info_edits")
_prev = _load("acinfo_latest_snapshot")

revision = "sheet_aw_ave_as_is"
down_revision = "acinfo_latest_snapshot"
branch_labels = None
depends_on = None


def _sheet(source_view: str) -> None:
    op.execute("DROP VIEW IF EXISTS forecast.detailed_aircraft_information")
    op.execute("DROP VIEW IF EXISTS forecast.detailed_aircraft_information_source")
    op.execute(source_view)
    op.execute(_edits.VIEW)
    _prev._grant("forecast.detailed_aircraft_information_source")
    _prev._grant("forecast.detailed_aircraft_information")


def upgrade() -> None:
    assert _prev._AW in _edits.SOURCE_VIEW
    _sheet(_edits.SOURCE_VIEW)


def downgrade() -> None:
    _sheet(_edits.SOURCE_VIEW.replace(_prev._AW, _prev._AW_NEW, 1))
