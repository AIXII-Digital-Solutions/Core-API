from datetime import datetime
from typing import Optional, List

from sqlalchemy import (
    DateTime, String, Integer, Boolean, ARRAY, Index, UniqueConstraint,
)

from .config import FlightAwareBase as Base
from sqlalchemy.orm import Mapped, mapped_column


class History(Base):
    """AeroAPI v4 ``GET /history/flights/{ident}`` — one row per historical flight leg (the
    ``BaseFlight`` / "Flight" object). A faithful 1:1 mapping of the response fields, with the
    origin/destination ``FlightAirportRef`` objects flattened into ``origin_*`` / ``destination_*``
    columns and the two codeshare arrays kept as ``text[]``.

    Natural key = ``fa_flight_id`` (FlightAware's per-leg id). A DIVERTED flight is returned by the
    API as a SECOND leg sharing the SAME ``fa_flight_id`` (its destination = the divert airport); the
    loader inserts ``ON CONFLICT (fa_flight_id) DO NOTHING``, so that rare second leg collapses into
    the first row — accepted here in exchange for watertight re-run idempotency. ``inbound_fa_flight_id``
    is documented by the API as NOT populated on the /history endpoint (kept for schema parity with the
    live ``/flights/{ident}`` object). Times are the ICAO OOOI events — ``*_out`` gate departure,
    ``*_off`` runway/wheels-off, ``*_on`` runway/wheels-on, ``*_in`` gate arrival — each with
    ``scheduled_``/``estimated_``/``actual_`` variants. ``route_distance`` is the PLANNED filed-route
    distance in statute miles (the API has no actual-flown distance on this object).

    Loaded by ``_admin/load_history_flightaware.py`` (per-registration fan-out over <=7-day windows).
    """
    __table_args__ = (
        # FlightAware's per-leg id — the idempotency key for re-runnable bulk loads
        UniqueConstraint("fa_flight_id", name="uq_flightaware_history_fa_flight_id"),
        # "all flights of this tail over time" (the per-registration fan-out consumer)
        Index("ix_flightaware_history_reg_off", "registration", "actual_off"),
        # created_at is inherited from BaseMixin -> index for load-batch / time-range scans
        Index("ix_flightaware_history_created_at", "created_at"),
    )

    # --- identity / operator ---
    ident: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    ident_icao: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    ident_iata: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    fa_flight_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    operator: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    operator_icao: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    operator_iata: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    flight_number: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    registration: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    atc_ident: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    inbound_fa_flight_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    codeshares: Mapped[Optional[List[str]]] = mapped_column(ARRAY(String), nullable=True)
    codeshares_iata: Mapped[Optional[List[str]]] = mapped_column(ARRAY(String), nullable=True)

    # --- flags ---
    blocked: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    diverted: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    cancelled: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    position_only: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    # --- origin (FlightAirportRef, flattened) ---
    origin_code: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    origin_code_icao: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    origin_code_iata: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    origin_code_lid: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    origin_timezone: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    origin_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    origin_city: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # --- destination (FlightAirportRef, flattened) ---
    destination_code: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    destination_code_icao: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    destination_code_iata: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    destination_code_lid: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    destination_timezone: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    destination_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    destination_city: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # --- plan / summary ---
    departure_delay: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)   # seconds, neg = early
    arrival_delay: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)     # seconds, neg = early
    filed_ete: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)         # seconds, runway-to-runway PLAN
    progress_percent: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    aircraft_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    route_distance: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)    # PLANNED, statute miles
    filed_airspeed: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)    # knots
    filed_altitude: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)    # 100s of feet
    route: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    baggage_claim: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    seats_cabin_business: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    seats_cabin_coach: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    seats_cabin_first: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    gate_origin: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    gate_destination: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    terminal_origin: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    terminal_destination: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    type: Mapped[Optional[str]] = mapped_column(String, nullable=True)               # General_Aviation | Airline

    # --- OOOI times (ISO-8601 UTC) ---
    scheduled_out: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    estimated_out: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    actual_out: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    scheduled_off: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    estimated_off: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    actual_off: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    scheduled_on: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    estimated_on: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    actual_on: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    scheduled_in: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    estimated_in: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    actual_in: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    actual_runway_off: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    actual_runway_on: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    foresight_predictions_available: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    # --- provenance (NOT an API field): the ident/registration we queried this row by ---
    queried_ident: Mapped[Optional[str]] = mapped_column(String, nullable=True)
