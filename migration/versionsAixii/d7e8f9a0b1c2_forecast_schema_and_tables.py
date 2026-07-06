"""forecast schema + working tables (history_1 / future_1 / final_1)

Revision ID: d7e8f9a0b1c2
Revises: c6d7e8f9a0b1
Create Date: 2026-07-02

The airline-forecast data-prep layer (see docs/airline forecast handoff prompt.md + predictive/).
A frontend request (operator + date) assembles a per-aircraft historical panel:

  * history_1 — Cirium fleet rows (per Registration x monthly `period`, Operator-scoped) LEFT-JOINed
    to their FR24 flights (flightradar.flightsummary) with a DATE-RESPECTING match (a flight only
    matches the Cirium period whose MONTH it falls in — a tail migrates between operators, so a
    09-2024 Cirium row must not pick up a 07-2025 flight). Cirium rows with no flight in their month
    are still kept here (null flight fields).
  * future_1 — identical columns; populated later by the forecast model (table only for now).
  * final_1  — history_1 (FLIGHTS ONLY: the no-flight Cirium rows are dropped here) UNION future_1,
    enriched with origin/destination airport geography from main.virtual_airport_list.

Columns use the same quoted mixed-case names as the Cirium/airport source tables (and the task spec).
These are hand-written working tables NOT mapped to any Base — env.py's include_object is a whitelist,
so Alembic autogenerate ignores the whole forecast schema (it will never try to drop these). Managed
only via this migration.
"""
from alembic import op

revision = "d7e8f9a0b1c2"
down_revision = "c6d7e8f9a0b1"
branch_labels = None
depends_on = None


# Shared column block for history_1 / future_1 (order matches the task spec).
_PANEL_COLS = """
    id                          bigserial PRIMARY KEY,
    "Registration"              text,
    "Period"                    text,
    "Date"                      date,
    "Time Departed"             timestamptz,
    "Time Landed"               timestamptz,
    "IATA Origin"               text,
    "IATA Destination"          text,
    "IATA Destination Actual"   text,
    "Operator"                  text,
    "Master Series"             text,
    "Manufacturer"              text,
    "Aircraft Sub Series"       text,
    "Primary Usage"             text,
    created_at                  timestamptz NOT NULL DEFAULT now()
"""

# final_1 = the panel columns + origin/destination airport geography.
_FINAL_EXTRA_COLS = """,
    "Origin Country"            text,
    "Origin City"               text,
    "Origin Airport Name"       text,
    "Destination Country"       text,
    "Destination City"          text,
    "Destination Airport Name"  text
"""


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS forecast")
    op.execute(f"CREATE TABLE forecast.history_1 ({_PANEL_COLS})")
    op.execute(f"CREATE TABLE forecast.future_1  ({_PANEL_COLS})")
    op.execute(f"CREATE TABLE forecast.final_1   ({_PANEL_COLS}{_FINAL_EXTRA_COLS})")

    # Light indexes for the assemble/merge scans (Operator filter, per-tail inspection).
    for tbl in ("history_1", "future_1", "final_1"):
        op.execute(f'CREATE INDEX ix_forecast_{tbl}_operator ON forecast.{tbl} ("Operator")')
        op.execute(f'CREATE INDEX ix_forecast_{tbl}_reg_period ON forecast.{tbl} ("Registration", "Period")')


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS forecast CASCADE")
