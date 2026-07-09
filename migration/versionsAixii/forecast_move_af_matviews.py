"""Move the forecast (archetype-feature) materialized views from schema `api` to `forecast`, renaming the
`af_` prefix to `acys_` to match the forecast namespace. ALTER … SET SCHEMA / RENAME is metadata-only
(no recompute); dependencies between the views are tracked by OID, so the DAG (acys_base ← the 9 leaves)
stays intact automatically.

  api.af_base              -> forecast.acys_base
  api.af_cell_daily_coarse -> forecast.acys_cell_daily_coarse
  api.af_cell_daily_fine   -> forecast.acys_cell_daily_fine
  api.af_cells_coarse      -> forecast.acys_cells_coarse
  api.af_cells_fine        -> forecast.acys_cells_fine
  api.af_s1_coarse         -> forecast.acys_s1_coarse
  api.af_s1_fine           -> forecast.acys_s1_fine
  api.af_s4_coarse         -> forecast.acys_s4_coarse
  api.af_s4_fine           -> forecast.acys_s4_fine
  api.af_tail_dormancy     -> forecast.acys_tail_dormancy

Revision ID: forecast_move_af_matviews
Revises: forecast_grouped_date
Create Date: 2026-07-09
"""
from alembic import op

revision = "forecast_move_af_matviews"
down_revision = "forecast_grouped_date"
branch_labels = None
depends_on = None

_MV = [
    "base", "cell_daily_coarse", "cell_daily_fine", "cells_coarse", "cells_fine",
    "s1_coarse", "s1_fine", "s4_coarse", "s4_fine", "tail_dormancy",
]


def upgrade() -> None:
    for name in _MV:
        op.execute(f'ALTER MATERIALIZED VIEW IF EXISTS api.af_{name} SET SCHEMA forecast')
        op.execute(f'ALTER MATERIALIZED VIEW IF EXISTS forecast.af_{name} RENAME TO acys_{name}')


def downgrade() -> None:
    for name in _MV:
        op.execute(f'ALTER MATERIALIZED VIEW IF EXISTS forecast.acys_{name} RENAME TO af_{name}')
        op.execute(f'ALTER MATERIALIZED VIEW IF EXISTS forecast.af_{name} SET SCHEMA api')
