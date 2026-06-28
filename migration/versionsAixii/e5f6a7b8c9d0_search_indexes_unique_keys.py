"""search indexes + composite unique keys for cirium/flightradar/aviationedge tables

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-06-28

Hand-written (kept off autogenerate to avoid the cosmetic index-name drift left by the earlier
table renames). Adds the natural composite UNIQUE keys + the composite search indexes designed
from the actual query/write sites.

NOTE: the UNIQUE constraints assume the target tables hold no pre-existing duplicates for those
keys (true on a fresh cluster). If a table already has duplicate rows for a key, de-duplicate
first — otherwise creating the constraint will (correctly) fail and roll back this revision.
"""
from alembic import op

revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # cirium.ciriumaircrafts — composites for the matview self-joins / reg+sn search.
    op.drop_index("ix_cirium_revision_serial", table_name="ciriumaircrafts", schema="cirium")
    op.create_index("ix_cirium_revision_serial", "ciriumaircrafts",
                    ["revision_id", "Registration", "Serial Number"], schema="cirium")
    op.create_index("ix_cirium_reg_serial", "ciriumaircrafts",
                    ["Registration", "Serial Number"], schema="cirium")

    # flightradar.flightsummary — natural flight key + aircraft-over-time index.
    op.create_unique_constraint("uq_flightsummary_natural", "flightsummary",
                                ["fr24_id", "flight", "reg", "callsign"], schema="flightradar")
    op.create_index("ix_flightsummary_reg_takeoff", "flightsummary",
                    ["reg", "datetime_takeoff"], schema="flightradar")

    # flightradar.livepositions — one position per flight per timestamp + latest-position index.
    op.create_unique_constraint("uq_livepositions_fr24_timestamp", "livepositions",
                                ["fr24_id", "timestamp"], schema="flightradar")
    op.create_index("ix_livepositions_reg_flight_created", "livepositions",
                    ["reg", "flight", "created_at"], schema="flightradar")

    # flightradar.airports — icao/iata become UNIQUE (replace the plain lookup indexes).
    op.drop_index("ix_flightradar_airports_iata", table_name="airports", schema="flightradar")
    op.drop_index("ix_flightradar_airports_icao", table_name="airports", schema="flightradar")
    op.create_unique_constraint("uq_airports_icao", "airports", ["icao"], schema="flightradar")
    op.create_unique_constraint("uq_airports_iata", "airports", ["iata"], schema="flightradar")

    # flightradar.airportrunways — one runway per (airport, designator).
    op.create_unique_constraint("uq_airportrunways_airport_designator", "airportrunways",
                                ["airport_id", "designator"], schema="flightradar")

    # aviationedge.historicalschedule — composite search indexes (route+date, flight+date).
    op.create_index("ix_histsched_dep_iata_time", "historicalschedule",
                    ["departure_iata_code", "departure_scheduled_time"], schema="aviationedge")
    op.create_index("ix_histsched_arr_iata_time", "historicalschedule",
                    ["arrival_iata_code", "arrival_scheduled_time"], schema="aviationedge")
    op.create_index("ix_histsched_flight_iata_time", "historicalschedule",
                    ["flight_iata_number", "departure_scheduled_time"], schema="aviationedge")


def downgrade() -> None:
    op.drop_index("ix_histsched_flight_iata_time", table_name="historicalschedule", schema="aviationedge")
    op.drop_index("ix_histsched_arr_iata_time", table_name="historicalschedule", schema="aviationedge")
    op.drop_index("ix_histsched_dep_iata_time", table_name="historicalschedule", schema="aviationedge")

    op.drop_constraint("uq_airportrunways_airport_designator", "airportrunways", schema="flightradar", type_="unique")

    op.drop_constraint("uq_airports_iata", "airports", schema="flightradar", type_="unique")
    op.drop_constraint("uq_airports_icao", "airports", schema="flightradar", type_="unique")
    op.create_index("ix_flightradar_airports_icao", "airports", ["icao"], schema="flightradar")
    op.create_index("ix_flightradar_airports_iata", "airports", ["iata"], schema="flightradar")

    op.drop_index("ix_livepositions_reg_flight_created", table_name="livepositions", schema="flightradar")
    op.drop_constraint("uq_livepositions_fr24_timestamp", "livepositions", schema="flightradar", type_="unique")

    op.drop_index("ix_flightsummary_reg_takeoff", table_name="flightsummary", schema="flightradar")
    op.drop_constraint("uq_flightsummary_natural", "flightsummary", schema="flightradar", type_="unique")

    op.drop_index("ix_cirium_reg_serial", table_name="ciriumaircrafts", schema="cirium")
    op.drop_index("ix_cirium_revision_serial", table_name="ciriumaircrafts", schema="cirium")
    op.create_index("ix_cirium_revision_serial", "ciriumaircrafts",
                    ["revision_id", "Serial Number"], schema="cirium")
