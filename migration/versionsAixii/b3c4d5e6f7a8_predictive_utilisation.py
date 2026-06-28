"""cirium.airlines matview + api.predictive_utilisation wide table + cleanup fn

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
Create Date: 2026-06-28

Stage 1 of the predictive-utilisation feature:
- cirium.airlines : MATERIALIZED VIEW, one row per distinct Operator (airline/icao/iata) over
  cirium.ciriumaircrafts (all revisions). Refreshed by external-worker.
- api.predictive_utilisation : WIDE table = all flightradar.flightsummary columns + the step-3.3
  aircraft fields (joined by reg), scoped per airline by airline_icao. The worker replaces a
  given airline's rows (DELETE WHERE airline_icao=...) on each run; an idle-cleanup job TRUNCATEs
  the whole table (reset ids) via api.cleanup_predictive_utilisation().
"""
from alembic import op
import sqlalchemy as sa

revision = "b3c4d5e6f7a8"
down_revision = "a2b3c4d5e6f7"
branch_labels = None
depends_on = None


AIRLINES_VIEW = """
CREATE MATERIALIZED VIEW cirium.airlines AS
SELECT DISTINCT ON ("Operator")
    "Operator"      AS airline,
    "Operator ICAO" AS icao,
    "Operator IATA" AS iata
FROM cirium.ciriumaircrafts
WHERE "Operator" IS NOT NULL
ORDER BY "Operator", ("Operator ICAO" IS NULL), ("Operator IATA" IS NULL)
WITH DATA
"""

CLEANUP_FN = """
CREATE OR REPLACE FUNCTION api.cleanup_predictive_utilisation() RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
BEGIN
    TRUNCATE api.predictive_utilisation RESTART IDENTITY;
END;
$$;
"""


def upgrade() -> None:
    op.execute(AIRLINES_VIEW)
    op.execute('CREATE UNIQUE INDEX ix_cirium_airlines_airline ON cirium.airlines (airline)')
    op.execute('CREATE INDEX ix_cirium_airlines_icao ON cirium.airlines (icao)')
    op.execute('CREATE INDEX ix_cirium_airlines_iata ON cirium.airlines (iata)')

    op.create_table(
        "predictive_utilisation",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("airline_icao", sa.String(), nullable=True),
        sa.Column("fr24_id", sa.String(), nullable=True),
        sa.Column("flight", sa.String(), nullable=True),
        sa.Column("callsign", sa.String(), nullable=True),
        sa.Column("operating_as", sa.String(), nullable=True),
        sa.Column("painted_as", sa.String(), nullable=True),
        sa.Column("type", sa.String(), nullable=True),
        sa.Column("reg", sa.String(), nullable=True),
        sa.Column("orig_icao", sa.String(), nullable=True),
        sa.Column("orig_iata", sa.String(), nullable=True),
        sa.Column("datetime_takeoff", sa.DateTime(timezone=True), nullable=True),
        sa.Column("runway_takeoff", sa.String(), nullable=True),
        sa.Column("dest_icao", sa.String(), nullable=True),
        sa.Column("dest_iata", sa.String(), nullable=True),
        sa.Column("dest_icao_actual", sa.String(), nullable=True),
        sa.Column("dest_iata_actual", sa.String(), nullable=True),
        sa.Column("datetime_landed", sa.DateTime(timezone=True), nullable=True),
        sa.Column("runway_landed", sa.String(), nullable=True),
        sa.Column("flight_time", sa.Integer(), nullable=True),
        sa.Column("actual_distance", sa.Float(), nullable=True),
        sa.Column("circle_distance", sa.Float(), nullable=True),
        sa.Column("category", sa.String(), nullable=True),
        sa.Column("hex", sa.String(), nullable=True),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column("flight_ended", sa.Boolean(), nullable=True),
        sa.Column("msn", sa.String(), nullable=True),
        sa.Column("airline", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("delivery_date", sa.Date(), nullable=True),
        sa.Column("in_service_date", sa.Date(), nullable=True),
        sa.Column("first_flight_date", sa.Date(), nullable=True),
        sa.Column("indicative_value", sa.Float(), nullable=True),
        sa.Column("num_of_seats", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        schema="api",
    )
    op.create_index("ix_predutil_airline_icao", "predictive_utilisation", ["airline_icao"], schema="api")
    op.create_index("ix_predutil_reg", "predictive_utilisation", ["reg"], schema="api")
    op.create_index("ix_predutil_reg_takeoff", "predictive_utilisation", ["reg", "datetime_takeoff"], schema="api")

    op.execute(CLEANUP_FN)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS api.cleanup_predictive_utilisation()")
    op.drop_table("predictive_utilisation", schema="api")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS cirium.airlines")
