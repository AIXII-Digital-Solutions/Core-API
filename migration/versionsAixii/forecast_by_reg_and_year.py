"""forecast.acys_summary_grouped_by_reg_and_year — acys_summary_grouped_by_reg rolled up one level further:
the MONTH is dropped, so the grain becomes the aircraft-YEAR (Registration x Contract Year x Data Type).

Why the CONTRACT year and not the calendar one: it is the model's own yearly axis (a fiscal window anchored at
the request date), the one the Agreed-Value columns are already shaped by — a calendar year would straddle two
of them.

The whole Agreed-Value family is RECOMPUTED from the year's own monthly series instead of being carried up:
by_reg's four CY-columns are computed per (Registration, Contract Year) over the WHOLE contract year, so the
Actuals half and the Forecast half of one CY would otherwise inherit the same four numbers no matter which
months the row covers. Here each follows its own formula over exactly its row's months — "Agreed Value" = the
MEAN of the monthly values (taken over MONTHS, so a month split across several by_reg rows cannot outweigh a
single-row month; "# Of Months" carries the n), inception / at-end = its first / last month, the weighted
average = (inception + end) / 2, the activity-weighted one = sum(value * flights) / sum(flights). The last
four skip wet and non-positive months, as the model does everywhere else; the mean does not. Flights /
distances / flight times SUM; "Age" takes MIN (as by_reg does); the descriptive columns take MAX.

No key columns: the grain columns (Registration, Contract Year, Data Type) ARE the key and carry the UNIQUE
index. Join the per-month dimension forecast.aircraft_information on "Registration".

The definition is IMPORTED from the source of truth forecast_grouped_route_cols._BY_REG_YEAR (updated in
lockstep, together with the chain's _drop_chain / _rebuild / owner / grants), so this migration duplicates no
SQL and cannot drift from it. The new matview READS by_reg, so the chain builder drops it BEFORE by_reg and
rebuilds it right AFTER — otherwise a future full chain rebuild would fail on the dependency.

external-worker REFRESHes it at the end of every forecast run, in dependency order right after
acys_summary_grouped_by_reg (panel.py step 10) — same as the rest of the report chain.

Revision ID: forecast_by_reg_and_year
Revises: d5a3f81c6b04
Create Date: 2026-09-09
"""
import os
import sys

from alembic import op

# the source-of-truth chain module lives beside this migration; put versionsAixii on sys.path so we can import
# the CANONICAL by_reg_and_year definition + its index list instead of copying the SQL.
sys.path.insert(0, os.path.dirname(__file__))
from forecast_grouped_route_cols import _BY_REG_YEAR, _BY_REG_YEAR_INDEXES  # noqa: E402

revision = "forecast_by_reg_and_year"
down_revision = "d5a3f81c6b04"
branch_labels = None
depends_on = None

_OBJ = "forecast.acys_summary_grouped_by_reg_and_year"

# REFRESH MATERIALIZED VIEW requires OWNERSHIP (no GRANT can hand it out), and the refresh runs from
# external-worker's role — so the matview belongs to grp_aviation_write, like the rest of the report chain.
_OWNER = f"""
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aviation_write') THEN
    EXECUTE 'ALTER MATERIALIZED VIEW {_OBJ} OWNER TO grp_aviation_write';
  END IF;
END $$;
"""

_GRANTS = f"""
DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['grp_aixii_read','grp_aviation_write'] LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('GRANT SELECT ON {_OBJ} TO %I', r);
    END IF;
  END LOOP;
END $$;
"""


def upgrade() -> None:
    op.execute(f"DROP MATERIALIZED VIEW IF EXISTS {_OBJ}")
    op.execute(_BY_REG_YEAR)
    for ix in _BY_REG_YEAR_INDEXES:
        op.execute(ix)
    op.execute(_OWNER)
    op.execute(_GRANTS)


def downgrade() -> None:
    # a LEAF matview — nothing reads it, so it just goes away (its indexes with it).
    op.execute(f"DROP MATERIALIZED VIEW IF EXISTS {_OBJ}")
