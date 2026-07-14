"""MERGED_KEY also on forecast.acys_summary_by_day and forecast.acys_summary_grouped_by_reg, so every layer
of the report joins to forecast.aircraft_information on the same key.

The expression is character-for-character the one already used by acys_summary_grouped and
aircraft_information — the four MUST agree or the join silently drops rows:

    "Registration" || '|' || coalesce("Aircraft Sub Series",'') || '|' || "Period"

On the TABLE it is GENERATED ... STORED: all three inputs are already columns there, so the 400k existing
rows are filled the moment the column is added, every future insert fills itself, and no worker code has to
know about it (the merge's INSERT lists its columns explicitly, and a generated column may not appear in
that list anyway).

acys_summary_grouped (the matview) is NOT rebuilt: it computes MERGED_KEY in its own SELECT, and a new
column on the base table does not change its definition.

Revision ID: forecast_merged_key_more
Revises: powerbi_zdates_cy_indata
Create Date: 2026-07-10
"""
from alembic import op

revision = "forecast_merged_key_more"
down_revision = "powerbi_zdates_cy_indata"
branch_labels = None
depends_on = None

_MERGED_KEY = """"Registration" || '|' || coalesce("Aircraft Sub Series",'') || '|' || "Period\""""

_BY_REG_KEYS = """    "Registration", "Period",
    "Operator", "Master Series", "Manufacturer", "Aircraft Sub Series", "Primary Usage",
    "Contract Year",
    "Agreed Value", "Total Seats", "Total PAX",
    "Delivery Date", "Lease Type", "Lease Dry Wet", "Operational Lessor",
    "Data Type",
    "Date\""""

_BY_REG = f"""
CREATE VIEW forecast.acys_summary_grouped_by_reg AS
SELECT
    {_MERGED_KEY} AS "MERGED_KEY",
{_BY_REG_KEYS},
    min("Age")                                     AS "Age",
    sum("# Of Flights")                            AS "# Of Flights",
    sum("Circle Distance")                         AS "Circle Distance",
    sum("Actual Distance FR")                      AS "Actual Distance FR",
    sum("Flight Time")                             AS "Flight Time",
    sum("Flight Time FR")                          AS "Flight Time FR",
    max("Agreed Value on Inception")               AS "Agreed Value on Inception",
    max("Agreed Value at the End of the Contract") AS "Agreed Value at the End of the Contract",
    max("Weighted Average Agreed Value")           AS "Weighted Average Agreed Value",
    max("Activity-Weighted Average Agreed Value")  AS "Activity-Weighted Average Agreed Value"
FROM forecast.acys_summary_grouped
GROUP BY
{_BY_REG_KEYS}
"""

# unchanged — recreated only because it depends on acys_summary_grouped_by_reg
_AIRCRAFT_INFO = f"""
CREATE VIEW forecast.aircraft_information AS
SELECT
    {_MERGED_KEY}             AS "MERGED_KEY",
    "Registration",
    "Period",
    "Aircraft Sub Series",
    max("Operator")           AS "Operator",
    max("Master Series")      AS "Master Series",
    max("Manufacturer")       AS "Manufacturer",
    max("Primary Usage")      AS "Primary Usage",
    max("Agreed Value")       AS "Agreed Value",
    max("Lease Type")         AS "Lease Type",
    max("Lease Dry Wet")      AS "Lease Dry Wet",
    max("Operational Lessor") AS "Operational Lessor"
FROM forecast.acys_summary_grouped_by_reg
GROUP BY "Registration", "Aircraft Sub Series", "Period"
"""

_BY_REG_PREV = _BY_REG.replace(f'    {_MERGED_KEY} AS "MERGED_KEY",\n', "")

_GRANTS = """
DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['grp_aixii_read','grp_aviation_write'] LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('GRANT SELECT ON forecast.acys_summary_grouped_by_reg, '
                     'forecast.aircraft_information TO %I', r);
    END IF;
  END LOOP;
END $$;
"""


def upgrade() -> None:
    op.execute(f'ALTER TABLE forecast.acys_summary_by_day '
               f'ADD COLUMN "MERGED_KEY" text GENERATED ALWAYS AS ({_MERGED_KEY}) STORED')
    op.execute('CREATE INDEX ix_acys_by_day_mkey ON forecast.acys_summary_by_day ("MERGED_KEY")')
    op.execute("DROP VIEW IF EXISTS forecast.aircraft_information")
    op.execute("DROP VIEW IF EXISTS forecast.acys_summary_grouped_by_reg")
    op.execute(_BY_REG)
    op.execute(_AIRCRAFT_INFO)
    op.execute(_GRANTS)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS forecast.aircraft_information")
    op.execute("DROP VIEW IF EXISTS forecast.acys_summary_grouped_by_reg")
    op.execute(_BY_REG_PREV)
    op.execute(_AIRCRAFT_INFO)
    op.execute(_GRANTS)
    op.execute("DROP INDEX IF EXISTS forecast.ix_acys_by_day_mkey")
    op.execute('ALTER TABLE forecast.acys_summary_by_day DROP COLUMN IF EXISTS "MERGED_KEY"')
