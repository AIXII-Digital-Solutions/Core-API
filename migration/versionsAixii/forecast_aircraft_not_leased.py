"""forecast.aircraft_information: NULL Lease Type / Lease Dry Wet / Operational Lessor -> 'Not Leased'.

These three are NULL for an owned (non-leased) aircraft — 2,643 of 3,860 aircraft-months have no lease
type, 2,869 have no lessor. In the report a blank reads as "missing", so they are relabelled 'Not Leased'.

Only aircraft_information is touched (the report's aircraft dimension); the underlying facts keep NULL.
The view is a leaf (nothing depends on it), so a plain DROP + CREATE — no dependency-chain rebuild.

coalesce(nullif(max(...),''), 'Not Leased'): the source currently holds only NULLs (no empty strings), but
nullif('') guards against an empty string appearing later so it, too, becomes 'Not Leased' rather than a
blank that slips through.

Revision ID: forecast_aircraft_not_leased
Revises: forecast_dateint_join
Create Date: 2026-07-10
"""
from alembic import op

revision = "forecast_aircraft_not_leased"
down_revision = "forecast_dateint_join"
branch_labels = None
depends_on = None

_MK = """"Registration" || '|' || coalesce("Aircraft Sub Series",'') || '|' || "Period\""""


def _view(not_leased: bool) -> str:
    def lease(col: str) -> str:
        inner = f'max("{col}")'
        return (f"coalesce(nullif({inner},''), 'Not Leased') AS \"{col}\"" if not_leased
                else f'{inner} AS "{col}"')
    return f"""
CREATE VIEW forecast.aircraft_information AS
SELECT
    {_MK}                     AS "MERGED_KEY",
    "Registration",
    "Period",
    "Aircraft Sub Series",
    max("Operator")           AS "Operator",
    max("Master Series")      AS "Master Series",
    max("Manufacturer")       AS "Manufacturer",
    max("Primary Usage")      AS "Primary Usage",
    max("Agreed Value")       AS "Agreed Value",
    {lease("Lease Type")},
    {lease("Lease Dry Wet")},
    {lease("Operational Lessor")}
FROM forecast.acys_summary_grouped_by_reg
GROUP BY "Registration", "Aircraft Sub Series", "Period"
"""


_GRANTS = """
DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['grp_aixii_read','grp_aviation_write'] LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('GRANT SELECT ON forecast.aircraft_information TO %I', r);
    END IF;
  END LOOP;
END $$;
"""


def upgrade() -> None:
    op.execute("DROP VIEW IF EXISTS forecast.aircraft_information")
    op.execute(_view(not_leased=True))
    op.execute(_GRANTS)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS forecast.aircraft_information")
    op.execute(_view(not_leased=False))
    op.execute(_GRANTS)
