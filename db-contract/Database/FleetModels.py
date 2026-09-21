"""The physical aircraft — schema `fleet` in the aixii database.

The airframe, its type and the engines bolted to it. This schema knows nothing about money: it
outlives every lease and every policy ever written about the aircraft, and both of those point
HERE rather than the other way round.

Not to be confused with the other two "fleet" notions in the platform, which answer a different
question and stay where they are:
  * `cirium.asg_commercial` / `cirium.non_asg_insured_*` — which tails to TRACK on FlightRadar,
    derived weekly from Cirium. A matview, rebuilt wholesale.
  * `api.registration` — the hand-kept list that feeds those matviews.
`fleet.aircraft` is neither: it is the durable record of an airframe this business has insured,
entered once and corrected in place, with every correction kept in `audit.change_log`.

Alembic reads THIS file (db-contract); `app/Database/FleetModels.py` is core-api's runtime copy.
"""
import inspect
import sys
from datetime import date
from typing import Optional, List

from sqlalchemy import (
    String, Text, BigInteger, Integer, Date, ForeignKey, Computed,
    UniqueConstraint, CheckConstraint, Index, text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .config import FleetBase as Base
# ref.airline lives on RefBase, in another MetaData. A ForeignKey STRING ("ref.airline.id") is
# resolved inside the OWNING metadata and cannot see another Base's table, so the cross-schema link
# is made with the Column object and the relationship names the class.
from .RefModels import Airline

MAX_ENGINES = 4


class AircraftType(Base):
    """A reusable aircraft type, e.g. Airbus / A320-232.

    `template_url` points at the outline drawing the portal overlays damage on — a URL into the
    platform's image store, never the bytes themselves. Keeping binaries out of the row means a
    grid can read a hundred types without dragging a hundred images through the connection.
    """
    __tablename__ = "aircraft_type"

    manufacturer: Mapped[Optional[str]] = mapped_column(String, nullable=True, default=None, index=True)
    master_series: Mapped[str] = mapped_column(String, nullable=False)
    master_series_normalized: Mapped[str] = mapped_column(
        String, Computed("upper(btrim(master_series))", persisted=True), nullable=False,
    )
    template_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True, default=None)

    __table_args__ = (
        UniqueConstraint("master_series_normalized", name="uq_aircraft_type_master_series"),
    )


class Aircraft(Base):
    """The airframe — the anchor the lease and the policy both hang off.

    Identity is the MSN, not the registration: a tail number changes when the aircraft changes
    operator or jurisdiction and can later be reissued to a different airframe, the manufacturer
    serial never does. Hence UNIQUE on msn (partial, so an unknown serial is still allowed) and
    only an index on registration.

    `registration` and `airline_id` are CURRENT state and are overwritten when the aircraft is
    re-registered or changes operator. Nothing is lost by that: the audit trigger records every
    such change with its date and actor, so "what was this airframe called in 2024" is a query
    against `audit.change_log`, and "who insured it then" is the coverage row for that period.

    `registration_normalized` (upper, separators stripped) makes lookups separator-insensitive —
    'YLLTD' finds 'YL-LTD'.
    """
    __tablename__ = "aircraft"

    registration: Mapped[str] = mapped_column(String, nullable=False, index=True)
    registration_normalized: Mapped[str] = mapped_column(
        String,
        Computed("upper(regexp_replace(registration, '[^A-Za-z0-9]', '', 'g'))", persisted=True),
        nullable=False,
    )
    msn: Mapped[Optional[str]] = mapped_column(String, nullable=True, default=None, index=True)
    aircraft_type_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("fleet.aircraft_type.id", ondelete="RESTRICT"),
        index=True, nullable=True, default=None,
    )
    airline_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey(Airline.__table__.c.id, ondelete="RESTRICT"),
        index=True, nullable=True, default=None,
    )

    aircraft_type: Mapped[Optional["AircraftType"]] = relationship("AircraftType", lazy="selectin")
    airline: Mapped[Optional["Airline"]] = relationship(Airline, lazy="selectin")
    engines: Mapped[List["AircraftEngine"]] = relationship(
        "AircraftEngine", back_populates="aircraft", lazy="selectin",
        order_by="(AircraftEngine.position, AircraftEngine.installed_on)",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_aircraft_registration_normalized", "registration_normalized"),
        Index("uq_aircraft_msn", "msn", unique=True, postgresql_where=text("msn IS NOT NULL")),
    )


class AircraftEngine(Base):
    """One engine, in one position, from the date it was installed.

    POSITION NUMBERING is the aircraft's own left-to-right as the PILOT sees it looking forward —
    not as somebody standing in front of the nose sees it, which reverses the sides:

        2 engines   1 = left wing,      2 = right wing               (737, A320)
        3 engines   1 = left,           2 = centre / tail,   3 = right
                    (L-1011, DC-10, MD-11, and the rear-engined Falcon 50 / 900 / 7X / 8X)
        4 engines   1 = left outboard,  2 = left inboard,
                    3 = right inboard,  4 = right outboard           (747, A380)

    A tail or centre engine sits on the centreline, so it takes the middle number.

    ENGINE SWAPS are new rows, not edits: engines are separately-serialised assets that move
    between airframes, so a position accumulates one row per installation and the fitted engine is
    the newest of them. There is no `installed_to` — a removal is implied by the next installation
    at that position, and the audit log records the row's own history either way. The currently
    fitted set is

        SELECT DISTINCT ON (aircraft_id, position) *
        FROM fleet.aircraft_engine
        ORDER BY aircraft_id, position, installed_on DESC NULLS LAST, id DESC

    `master_series` is the engine model as written, e.g. 'CFM56-5B4/3' — plain text rather than a
    reference table, because the series is descriptive here and never joined on.
    """
    __tablename__ = "aircraft_engine"

    aircraft_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("fleet.aircraft.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    master_series: Mapped[Optional[str]] = mapped_column(String, nullable=True, default=None)
    msn: Mapped[Optional[str]] = mapped_column(String, nullable=True, default=None, index=True)
    installed_on: Mapped[Optional[date]] = mapped_column(Date, nullable=True, default=None)
    details: Mapped[Optional[str]] = mapped_column(Text, nullable=True, default=None)

    aircraft: Mapped["Aircraft"] = relationship("Aircraft", back_populates="engines")

    __table_args__ = (
        CheckConstraint(f"position BETWEEN 1 AND {MAX_ENGINES}", name="ck_aircraft_engine_position"),
        # One installation per position per date. NULLS NOT DISTINCT so a position cannot collect
        # two undated rows, which would leave "which one is fitted" undecidable.
        UniqueConstraint(
            "aircraft_id", "position", "installed_on",
            name="uq_aircraft_engine_installation", postgresql_nulls_not_distinct=True,
        ),
    )


_current_module = sys.modules[__name__]

__all__ = [
    name
    for name, obj in globals().items()
    if inspect.isclass(obj) and obj.__module__ == __name__
]
