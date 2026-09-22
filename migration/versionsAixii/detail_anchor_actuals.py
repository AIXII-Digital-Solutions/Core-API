"""detailed_aircraft_information: one row per aircraft and contract year, facts winning the anchor

The view's grain is (Registration, Contract Year, **Data Type**), so the contract year the run's
as-of date falls in — the ANCHOR year — carries two rows: the part already flown (`Actuals`) and
the rest of the year (`Forecast`). Correct, and unreadable in a report that does not show the Data
Type column: the same tail appears twice, identical but for one figure, and looks duplicated.

It is usually `Agreed Value / AW AVE` alone that differs, which makes it look even more like a
glitch. That column weights the agreed value by ACTIVITY, so it moves when the flights move even
though the value itself has not: SU-RSA reads 18.91 over the flown months and 18.70 over the
forecast ones while INC / AVE / EXP stay 18.70 across both. Where an aircraft's agreed value is not
flat across the year, the other three diverge too.

So the view now collapses to ONE row per (Registration, Contract Year), and in the anchor year that
row is the FACTS. Past years were only ever Actuals and future years only ever Forecast, so nothing
changes for them — this decides the anchor year alone.

WHAT THIS GIVES UP, deliberately: in the anchor year the figures now describe the months already
flown, not the whole year. That is what "the row that relates to the facts" means, and it is the
right default for a sheet people read to see what an aircraft IS. The forecast half is not lost —
`forecast.acys_summary_grouped_by_reg_and_year` still carries both rows, and any report that wants
the full year can read it there.

`Data Type` stays in the output. It is no longer a splitter, it is the label that says whether that
year's row is fact or projection, which is exactly what a reader needs to know once the two are no
longer side by side.

Revision ID: detail_anchor_actuals
Revises: powerbi_read_grants
Create Date: 2026-09-22
"""
import importlib.util
import pathlib

from alembic import op

# Alembic imports a revision as a standalone module, so the sibling that owns the view text is
# loaded by file path rather than copied — 60 lines of SQL duplicated here would drift.
_spec = importlib.util.spec_from_file_location(
    "_forecast_grouped_route_cols",
    pathlib.Path(__file__).with_name("forecast_grouped_route_cols.py"))
_prev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_prev)

revision = "detail_anchor_actuals"
down_revision = "powerbi_read_grants"
branch_labels = None
depends_on = None

_PREV_VIEW = _prev._detailed_ac_info_guarded()

# The whole change: pick one row per (Registration, Contract Year) before the joins, Actuals first.
# `("Data Type" = 'Actuals') DESC` puts true ahead of false; within a pair there are at most those
# two rows, and Data Type is the final tiebreak so the choice is deterministic either way.
_FROM = "FROM forecast.acys_summary_grouped_by_reg_and_year y"
_FROM_COLLAPSED = """FROM (
    SELECT DISTINCT ON ("Registration", "Contract Year") *
    FROM forecast.acys_summary_grouped_by_reg_and_year
    ORDER BY "Registration", "Contract Year", ("Data Type" = 'Actuals') DESC, "Data Type"
) y"""

VIEW = _PREV_VIEW.replace(_FROM, _FROM_COLLAPSED, 1)


def upgrade() -> None:
    assert _FROM in _PREV_VIEW, "the previous view no longer reads by_reg_and_year as `y`"
    assert _FROM_COLLAPSED in VIEW
    op.execute("DROP VIEW IF EXISTS forecast.detailed_aircraft_information")
    op.execute(VIEW)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS forecast.detailed_aircraft_information")
    op.execute(_PREV_VIEW)
