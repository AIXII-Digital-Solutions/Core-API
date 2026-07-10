"""Create forecast.acys_forecast_coefficients — the coefficients that DRIVE the forecast, one row per
(Operator, Master Series, Forecast Month), so the portal can chart "how the forecast is computed":
seasonal curve, recent level, per-aircraft rate, active-fleet growth, and the resulting forecast volume
(Forecast Flights = Flights Per Aircraft × Active Fleet). Written by external-worker's forecast model
(per-operator refresh: it DELETEs its operator's rows then re-inserts). Prefix acys_, forecast schema.

Revision ID: forecast_coefficients_table
Revises: forecast_grouped_wavg_midpoint
Create Date: 2026-07-10
"""
from alembic import op

revision = "forecast_coefficients_table"
down_revision = "forecast_grouped_wavg_midpoint"
branch_labels = None
depends_on = None

_TABLE = """
CREATE TABLE forecast.acys_forecast_coefficients (
    id                     bigserial PRIMARY KEY,
    "Operator"             text    NOT NULL,
    "Master Series"        text,                       -- broader type (for chart grouping)
    "Aircraft Sub Series"  text    NOT NULL,          -- the forecast SUB-FLEET key (fit/fleet/routes group by this)
    "Forecast Month"       date    NOT NULL,          -- 1st of the forecast month
    "Calendar Month"       integer NOT NULL,          -- 1..12 (month-of-year, for the seasonal curve)
    "Frontier"             date,                       -- last complete actual month (the fit boundary)
    "Level"                double precision,           -- deseasonalized recent flight level (sub-fleet)
    "Base Fleet"           double precision,           -- typical flown-tail count (sub-fleet)
    "Per Aircraft Rate"    double precision,           -- Level / Base Fleet (deseasonalized flights per aircraft)
    "Seasonal Factor"      double precision,           -- seasonal[Calendar Month]
    "Proration"            double precision,           -- covered-days fraction (1.0 except current & final months)
    "Active Fleet"         integer,                    -- aircraft flying this month (delivered <= month)
    "Flights Per Aircraft" integer,                    -- k = round(Per Aircraft Rate × Seasonal Factor × Proration)
    "Forecast Flights"     integer,                    -- k × Active Fleet (the month's forecast volume)
    "Template Month"       date,                       -- route-template month used for the type's network
    created_at             timestamptz NOT NULL DEFAULT now()
)
"""

_GRANT = """
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aixii_read') THEN
    GRANT SELECT ON forecast.acys_forecast_coefficients TO grp_aixii_read;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aviation_write') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON forecast.acys_forecast_coefficients TO grp_aviation_write;
    GRANT USAGE, SELECT ON SEQUENCE forecast.acys_forecast_coefficients_id_seq TO grp_aviation_write;
  END IF;
END $$;
"""


def upgrade() -> None:
    op.execute(_TABLE)
    op.execute('CREATE INDEX ix_acys_fc_coeff_operator ON forecast.acys_forecast_coefficients ("Operator")')
    op.execute('CREATE INDEX ix_acys_fc_coeff_op_sf ON forecast.acys_forecast_coefficients ("Operator", "Aircraft Sub Series")')
    op.execute(_GRANT)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS forecast.acys_forecast_coefficients")
