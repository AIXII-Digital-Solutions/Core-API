import inspect
import sys
from datetime import datetime, date

from sqlalchemy import String, BigInteger, ForeignKey, Integer, Float, Boolean, Date, DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .config import ApiBase as Base


# Airlines reference list -> `api.airlines`. Mirrors MainModels.Airlines (airline_name + icao)
# with an extra `iata` column (same shape as `icao`). This is the source the asgaircraft
# (materialized) view will join against, so it lives in the aixii database (schema `api`).
class Airlines(Base):
    airline_name: Mapped[str] = mapped_column(String, index=True)
    icao: Mapped[str] = mapped_column(String, index=True)
    iata: Mapped[str] = mapped_column(String, index=True)


# Active aircraft taken from cirium.asg (is_active = true). NOT hand-maintained: the table is
# rebuilt by the DB function api.sync_registration_from_asg() after every cirium.asg refresh
# (external-worker calls it right after the asg matview REFRESH). `airline` resolves the airline
# name matched in asg to the api.airlines row.
class Registration(Base):
    reg: Mapped[str] = mapped_column(String, index=True)                 # Registration
    msn: Mapped[str] = mapped_column(String, index=True, nullable=True, default=None)  # Serial Number
    airline_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("api.airlines.id", ondelete="SET NULL"),
        index=True, nullable=True, default=None,
    )
    airline: Mapped["Airlines"] = relationship("Airlines", lazy="selectin")


class PredictiveUtilisation(Base):
    """Stage-1 predictive-utilisation rows: each flightradar.flightsummary row in the past
    window joined with its step-3.3 aircraft fields, scoped per airline by airline_icao.
    Filled/replaced by external-worker (raw SQL); read-back endpoint is a later stage."""
    airline_icao: Mapped[str] = mapped_column(String, index=True, nullable=True, default=None)

    # --- flightradar.flightsummary columns ---
    fr24_id: Mapped[str] = mapped_column(String, nullable=True, default=None)
    flight: Mapped[str] = mapped_column(String, nullable=True, default=None)
    callsign: Mapped[str] = mapped_column(String, nullable=True, default=None)
    operating_as: Mapped[str] = mapped_column(String, nullable=True, default=None)
    painted_as: Mapped[str] = mapped_column(String, nullable=True, default=None)
    type: Mapped[str] = mapped_column(String, nullable=True, default=None)
    reg: Mapped[str] = mapped_column(String, nullable=True, default=None)
    orig_icao: Mapped[str] = mapped_column(String, nullable=True, default=None)
    orig_iata: Mapped[str] = mapped_column(String, nullable=True, default=None)
    datetime_takeoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    runway_takeoff: Mapped[str] = mapped_column(String, nullable=True, default=None)
    dest_icao: Mapped[str] = mapped_column(String, nullable=True, default=None)
    dest_iata: Mapped[str] = mapped_column(String, nullable=True, default=None)
    dest_icao_actual: Mapped[str] = mapped_column(String, nullable=True, default=None)
    dest_iata_actual: Mapped[str] = mapped_column(String, nullable=True, default=None)
    datetime_landed: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    runway_landed: Mapped[str] = mapped_column(String, nullable=True, default=None)
    flight_time: Mapped[int] = mapped_column(Integer, nullable=True, default=None)
    actual_distance: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    circle_distance: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    category: Mapped[str] = mapped_column(String, nullable=True, default=None)
    hex: Mapped[str] = mapped_column(String, nullable=True, default=None)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    flight_ended: Mapped[bool] = mapped_column(Boolean, nullable=True, default=None)

    # --- step-3.3 aircraft fields (cirium.ciriumaircrafts, joined by reg) ---
    msn: Mapped[str] = mapped_column(String, nullable=True, default=None)
    airline: Mapped[str] = mapped_column(String, nullable=True, default=None)
    status: Mapped[str] = mapped_column(String, nullable=True, default=None)
    delivery_date: Mapped[date] = mapped_column(Date, nullable=True, default=None)
    in_service_date: Mapped[date] = mapped_column(Date, nullable=True, default=None)
    first_flight_date: Mapped[date] = mapped_column(Date, nullable=True, default=None)
    indicative_value: Mapped[float] = mapped_column(Float, nullable=True, default=None)
    num_of_seats: Mapped[int] = mapped_column(Integer, nullable=True, default=None)


_current_module = sys.modules[__name__]

__all__ = [
    name
    for name, obj in globals().items()
    if inspect.isclass(obj) and obj.__module__ == __name__
]
