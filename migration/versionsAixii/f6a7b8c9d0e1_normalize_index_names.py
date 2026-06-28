"""normalize drifted auto-generated index names after table renames

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-06-28

Hand-written. Several aixii tables were renamed (singular<->plural) but their op.f()-style
indexes kept the OLD table name in the index name, leaving cosmetic index-name drift that a
future `alembic --autogenerate` would try to "fix". This renames each drifted PLAIN index to the
model-expected name (paired by ordered column set, so truncation/spaces are handled). PKs,
sequences and unique-constraint-backed indexes are intentionally left untouched.
"""
from alembic import op

revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None

# (schema, old_index_name, new_index_name)
RENAMES = [
    ('airlabs', 'ix_airlabs_aircraftstates_airline_iata', 'ix_airlabs_aircraftstate_airline_iata'),
    ('airlabs', 'ix_airlabs_aircraftstates_airline_icao', 'ix_airlabs_aircraftstate_airline_icao'),
    ('airlabs', 'ix_airlabs_aircraftstates_reg_number', 'ix_airlabs_aircraftstate_reg_number'),
    ('api', 'ix_api_airline_icao', 'ix_api_airlines_icao'),
    ('api', 'ix_api_airline_airline_name', 'ix_api_airlines_airline_name'),
    ('api', 'ix_api_airline_iata', 'ix_api_airlines_iata'),
    ('aviationedge', 'ix_aviationedge_historicalschedules_airline_iata_code', 'ix_aviationedge_historicalschedule_airline_iata_code'),
    ('aviationedge', 'ix_aviationedge_historicalschedules_codeshared_airline__e2ec', 'ix_aviationedge_historicalschedule_codeshared_airline_icao_code'),
    ('aviationedge', 'ix_aviationedge_historicalschedules_arrival_icao_code', 'ix_aviationedge_historicalschedule_arrival_icao_code'),
    ('aviationedge', 'ix_aviationedge_historicalschedules_departure_iata_code', 'ix_aviationedge_historicalschedule_departure_iata_code'),
    ('aviationedge', 'ix_aviationedge_historicalschedules_codeshared_flight_i_3664', 'ix_aviationedge_historicalschedule_codeshared_flight_ic_b717'),
    ('aviationedge', 'ix_aviationedge_historicalschedules_codeshared_airline__52a6', 'ix_aviationedge_historicalschedule_codeshared_airline_iata_code'),
    ('aviationedge', 'ix_aviationedge_historicalschedules_flight_icao_number', 'ix_aviationedge_historicalschedule_flight_icao_number'),
    ('aviationedge', 'ix_aviationedge_historicalschedules_departure_icao_code', 'ix_aviationedge_historicalschedule_departure_icao_code'),
    ('aviationedge', 'ix_aviationedge_historicalschedules_codeshared_flight_i_1384', 'ix_aviationedge_historicalschedule_codeshared_flight_ia_fb19'),
    ('aviationedge', 'ix_aviationedge_historicalschedules_flight_iata_number', 'ix_aviationedge_historicalschedule_flight_iata_number'),
    ('aviationedge', 'ix_aviationedge_historicalschedules_airline_icao_code', 'ix_aviationedge_historicalschedule_airline_icao_code'),
    ('aviationedge', 'ix_aviationedge_historicalschedules_arrival_iata_code', 'ix_aviationedge_historicalschedule_arrival_iata_code'),
    ('cirium', 'ix_cirium_aircraftrevisions_revision_number', 'ix_cirium_aircraftrevision_revision_number'),
    ('cirium', 'ix_cirium_ciriumaircraft_Serial Number', 'ix_cirium_ciriumaircrafts_Serial Number'),
    ('cirium', 'ix_cirium_ciriumaircraft_revision_id', 'ix_cirium_ciriumaircrafts_revision_id'),
    ('cirium', 'ix_cirium_ciriumaircraft_Registration', 'ix_cirium_ciriumaircrafts_Registration'),
    ('flightradar', 'ix_flightradar_flightsummaries_reg', 'ix_flightradar_flightsummary_reg'),
    ('flightradar', 'ix_flightradar_flightsummaries_painted_as', 'ix_flightradar_flightsummary_painted_as'),
    ('flightradar', 'ix_flightradar_flightsummaries_operating_as', 'ix_flightradar_flightsummary_operating_as'),
    ('flightradar', 'ix_flightradar_flightsummaries_dest_iata_actual', 'ix_flightradar_flightsummary_dest_iata_actual'),
    ('flightradar', 'ix_flightradar_flightsummaries_orig_icao', 'ix_flightradar_flightsummary_orig_icao'),
    ('flightradar', 'ix_flightradar_flightsummaries_type', 'ix_flightradar_flightsummary_type'),
    ('flightradar', 'ix_flightradar_flightsummaries_orig_iata', 'ix_flightradar_flightsummary_orig_iata'),
    ('flightradar', 'ix_flightradar_flightsummaries_dest_icao', 'ix_flightradar_flightsummary_dest_icao'),
    ('flightradar', 'ix_flightradar_flightsummaries_dest_iata', 'ix_flightradar_flightsummary_dest_iata'),
    ('flightradar', 'ix_flightradar_flightsummaries_dest_icao_actual', 'ix_flightradar_flightsummary_dest_icao_actual'),
    ('flightradar', 'ix_flightradar_liveposition_dest_iata', 'ix_flightradar_livepositions_dest_iata'),
    ('flightradar', 'ix_flightradar_liveposition_orig_icao', 'ix_flightradar_livepositions_orig_icao'),
    ('flightradar', 'ix_flightradar_liveposition_dest_icao', 'ix_flightradar_livepositions_dest_icao'),
    ('flightradar', 'ix_flightradar_liveposition_reg', 'ix_flightradar_livepositions_reg'),
    ('flightradar', 'ix_flightradar_liveposition_operating_as', 'ix_flightradar_livepositions_operating_as'),
    ('flightradar', 'ix_flightradar_liveposition_painted_as', 'ix_flightradar_livepositions_painted_as'),
    ('flightradar', 'ix_flightradar_liveposition_orig_iata', 'ix_flightradar_livepositions_orig_iata'),
]


def upgrade() -> None:
    for schema, old, new in RENAMES:
        op.execute(f'ALTER INDEX "{schema}"."{old}" RENAME TO "{new}"')


def downgrade() -> None:
    for schema, old, new in reversed(RENAMES):
        op.execute(f'ALTER INDEX "{schema}"."{new}" RENAME TO "{old}"')
