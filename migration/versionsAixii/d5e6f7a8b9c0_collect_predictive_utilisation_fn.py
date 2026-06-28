"""api.collect_predictive_utilisation() SECURITY DEFINER function

Revision ID: d5e6f7a8b9c0
Revises: b3c4d5e6f7a8
Create Date: 2026-06-28

Move the predictive-utilisation "collect" (DELETE this airline's rows + wide INSERT..SELECT of its
active aircraft's flightsummary in the window) into a SECURITY DEFINER function, like
api.sync_registration_from_asg() / api.cleanup_predictive_utilisation(). external-worker then only
needs EXECUTE (PUBLIC) + USAGE on schema api — no direct INSERT/DELETE grant on the table.
"""
from alembic import op

revision = "d5e6f7a8b9c0"
down_revision = "b3c4d5e6f7a8"
branch_labels = None
depends_on = None


COLLECT_FN = """
CREATE OR REPLACE FUNCTION api.collect_predictive_utilisation(
    p_icao text, p_iata text, p_start timestamptz, p_end timestamptz
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
BEGIN
    DELETE FROM api.predictive_utilisation WHERE airline_icao = p_icao;

    INSERT INTO api.predictive_utilisation
      (airline_icao, fr24_id, flight, callsign, operating_as, painted_as, type, reg, orig_icao, orig_iata,
       datetime_takeoff, runway_takeoff, dest_icao, dest_iata, dest_icao_actual, dest_iata_actual,
       datetime_landed, runway_landed, flight_time, actual_distance, circle_distance, category, hex,
       first_seen, last_seen, flight_ended,
       msn, airline, status, delivery_date, in_service_date, first_flight_date, indicative_value, num_of_seats)
    SELECT p_icao,
      fs.fr24_id, fs.flight, fs.callsign, fs.operating_as, fs.painted_as, fs.type, fs.reg, fs.orig_icao, fs.orig_iata,
      fs.datetime_takeoff, fs.runway_takeoff, fs.dest_icao, fs.dest_iata, fs.dest_icao_actual, fs.dest_iata_actual,
      fs.datetime_landed, fs.runway_landed, fs.flight_time, fs.actual_distance, fs.circle_distance, fs.category, fs.hex,
      fs.first_seen, fs.last_seen, fs.flight_ended,
      ac."Serial Number", ac."Operator", ac."Status", ac."Delivery Date", ac."In Service Date",
      ac."First Flight Date", ac."Indicative Market Value (US$m)", ac."Number of Seats"
    FROM flightradar.flightsummary fs
    JOIN (
      SELECT DISTINCT ON ("Registration")
         "Registration", "Serial Number", "Operator", "Status", "Delivery Date",
         "In Service Date", "First Flight Date", "Indicative Market Value (US$m)", "Number of Seats"
      FROM cirium.ciriumaircrafts
      WHERE ("Operator ICAO" = p_icao OR "Operator IATA" = p_iata)
        AND ("Status" IS NULL OR "Status" NOT IN ('On order', 'On option'))
      ORDER BY "Registration", revision_id DESC
    ) ac ON ac."Registration" = fs.reg
    WHERE fs.datetime_takeoff >= p_start AND fs.datetime_takeoff < p_end;
END;
$$;
"""


def upgrade() -> None:
    op.execute(COLLECT_FN)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS api.collect_predictive_utilisation(text, text, timestamptz, timestamptz)")
