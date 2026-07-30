"""flightaware schema + history table (AeroAPI GET /history/flights/{ident})

Revision ID: flightaware_history
Revises: aircraft_info_cirium_fields
Create Date: 2026-07-25

Adds the `flightaware` aviation schema and its first table, `flightaware.history`, a faithful 1:1
mapping of the AeroAPI v4 `GET /history/flights/{ident}` Flight (BaseFlight) object — one row per
historical flight leg, with the origin/destination FlightAirportRef objects flattened into
`origin_*` / `destination_*` columns and the codeshare arrays kept as text[]. Natural key =
`fa_flight_id` (idempotent bulk re-loads via ON CONFLICT DO NOTHING).

Table is mapped to `FlightAwareBase` (db-contract/Database/FlightAwareModels.py) and IS part of the
Alembic aixii target, so this create_table matches what autogenerate emits — a later
`python tools/migrate.py revision aixii ...` should show NO diff for it. The `CREATE SCHEMA` is
hand-added (autogenerate never emits it), mirroring how the `forecast` schema was created in its own
migration (d7e8f9a0b1c2).

Loaded by `_admin/load_history_flightaware.py`.
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "flightaware_history"
down_revision = "aircraft_info_cirium_fields"
branch_labels = None
depends_on = None

# Light, guarded grants (mirrors flightsummary_coverage): the platform read/write roles get access if
# they exist. The _admin loader connects as the DB owner, so these are for the app/worker consumers.
_GRANTS = """
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aviation_write') THEN
    GRANT USAGE ON SCHEMA flightaware TO grp_aviation_write;
    GRANT SELECT, INSERT, UPDATE, DELETE ON flightaware.history TO grp_aviation_write;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aixii_read') THEN
    GRANT USAGE ON SCHEMA flightaware TO grp_aixii_read;
    GRANT SELECT ON flightaware.history TO grp_aixii_read;
  END IF;
END $$;
"""


def upgrade() -> None:
    op.execute('CREATE SCHEMA IF NOT EXISTS "flightaware"')

    op.create_table(
        "history",
        # --- identity / operator ---
        sa.Column("ident", sa.String(), nullable=True),
        sa.Column("ident_icao", sa.String(), nullable=True),
        sa.Column("ident_iata", sa.String(), nullable=True),
        sa.Column("fa_flight_id", sa.String(), nullable=True),
        sa.Column("operator", sa.String(), nullable=True),
        sa.Column("operator_icao", sa.String(), nullable=True),
        sa.Column("operator_iata", sa.String(), nullable=True),
        sa.Column("flight_number", sa.String(), nullable=True),
        sa.Column("registration", sa.String(), nullable=True),
        sa.Column("atc_ident", sa.String(), nullable=True),
        sa.Column("inbound_fa_flight_id", sa.String(), nullable=True),
        sa.Column("codeshares", sa.ARRAY(sa.String()), nullable=True),
        sa.Column("codeshares_iata", sa.ARRAY(sa.String()), nullable=True),
        # --- flags ---
        sa.Column("blocked", sa.Boolean(), nullable=True),
        sa.Column("diverted", sa.Boolean(), nullable=True),
        sa.Column("cancelled", sa.Boolean(), nullable=True),
        sa.Column("position_only", sa.Boolean(), nullable=True),
        # --- origin (FlightAirportRef, flattened) ---
        sa.Column("origin_code", sa.String(), nullable=True),
        sa.Column("origin_code_icao", sa.String(), nullable=True),
        sa.Column("origin_code_iata", sa.String(), nullable=True),
        sa.Column("origin_code_lid", sa.String(), nullable=True),
        sa.Column("origin_timezone", sa.String(), nullable=True),
        sa.Column("origin_name", sa.String(), nullable=True),
        sa.Column("origin_city", sa.String(), nullable=True),
        # --- destination (FlightAirportRef, flattened) ---
        sa.Column("destination_code", sa.String(), nullable=True),
        sa.Column("destination_code_icao", sa.String(), nullable=True),
        sa.Column("destination_code_iata", sa.String(), nullable=True),
        sa.Column("destination_code_lid", sa.String(), nullable=True),
        sa.Column("destination_timezone", sa.String(), nullable=True),
        sa.Column("destination_name", sa.String(), nullable=True),
        sa.Column("destination_city", sa.String(), nullable=True),
        # --- plan / summary ---
        sa.Column("departure_delay", sa.Integer(), nullable=True),
        sa.Column("arrival_delay", sa.Integer(), nullable=True),
        sa.Column("filed_ete", sa.Integer(), nullable=True),
        sa.Column("progress_percent", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("aircraft_type", sa.String(), nullable=True),
        sa.Column("route_distance", sa.Integer(), nullable=True),
        sa.Column("filed_airspeed", sa.Integer(), nullable=True),
        sa.Column("filed_altitude", sa.Integer(), nullable=True),
        sa.Column("route", sa.String(), nullable=True),
        sa.Column("baggage_claim", sa.String(), nullable=True),
        sa.Column("seats_cabin_business", sa.Integer(), nullable=True),
        sa.Column("seats_cabin_coach", sa.Integer(), nullable=True),
        sa.Column("seats_cabin_first", sa.Integer(), nullable=True),
        sa.Column("gate_origin", sa.String(), nullable=True),
        sa.Column("gate_destination", sa.String(), nullable=True),
        sa.Column("terminal_origin", sa.String(), nullable=True),
        sa.Column("terminal_destination", sa.String(), nullable=True),
        sa.Column("type", sa.String(), nullable=True),
        # --- OOOI times ---
        sa.Column("scheduled_out", sa.DateTime(timezone=True), nullable=True),
        sa.Column("estimated_out", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actual_out", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scheduled_off", sa.DateTime(timezone=True), nullable=True),
        sa.Column("estimated_off", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actual_off", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scheduled_on", sa.DateTime(timezone=True), nullable=True),
        sa.Column("estimated_on", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actual_on", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scheduled_in", sa.DateTime(timezone=True), nullable=True),
        sa.Column("estimated_in", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actual_in", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actual_runway_off", sa.String(), nullable=True),
        sa.Column("actual_runway_on", sa.String(), nullable=True),
        sa.Column("foresight_predictions_available", sa.Boolean(), nullable=True),
        # --- provenance ---
        sa.Column("queried_ident", sa.String(), nullable=True),
        # --- BaseMixin ---
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fa_flight_id", name="uq_flightaware_history_fa_flight_id"),
        schema="flightaware",
    )
    op.create_index("ix_flightaware_history_reg_off", "history",
                    ["registration", "actual_off"], unique=False, schema="flightaware")
    op.create_index("ix_flightaware_history_created_at", "history",
                    ["created_at"], unique=False, schema="flightaware")

    op.execute(_GRANTS)


def downgrade() -> None:
    op.drop_index("ix_flightaware_history_created_at", table_name="history", schema="flightaware")
    op.drop_index("ix_flightaware_history_reg_off", table_name="history", schema="flightaware")
    op.drop_table("history", schema="flightaware")
    op.execute('DROP SCHEMA IF EXISTS "flightaware" CASCADE')
