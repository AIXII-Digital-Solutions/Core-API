"""flightradar.flightsummary_coverage — the FR24 fetch coverage ledger.

Records, per registration, which [covered_from, covered_to] date ranges have already been fetched
from FR24 (so whatever flights exist for them are in flightsummary). The forecast panel fetches ONLY
the request window MINUS this ledger (the missing ranges), then records each fetched range — even if
it returned no flights — so it is never re-fetched. Bounds FR24 token spend to genuinely-new ranges.

Revision ID: flightsummary_coverage
Revises: forecast_summary_split
Create Date: 2026-07-08
"""
from alembic import op

revision = "flightsummary_coverage"
down_revision = "forecast_summary_split"
branch_labels = None
depends_on = None

_GRANTS = """
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aviation_write') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON flightradar.flightsummary_coverage TO grp_aviation_write;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aixii_read') THEN
    GRANT SELECT ON flightradar.flightsummary_coverage TO grp_aixii_read;
  END IF;
END $$;
"""


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS flightradar.flightsummary_coverage (
            reg          text NOT NULL,
            covered_from date NOT NULL,
            covered_to   date NOT NULL,
            updated_at   timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (reg, covered_from)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_flightsummary_coverage_reg "
               "ON flightradar.flightsummary_coverage (reg)")
    op.execute(_GRANTS)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS flightradar.flightsummary_coverage")
