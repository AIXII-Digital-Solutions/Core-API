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
from enum import Enum as PyEnum
from typing import Optional, List

from sqlalchemy import (
    String, Text, BigInteger, Integer, Date, Boolean, ForeignKey, Computed, Enum,
    UniqueConstraint, CheckConstraint, Index, text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .config import FleetBase as Base
# ref.airline lives on RefBase, in another MetaData. A ForeignKey STRING ("ref.airline.id") is
# resolved inside the OWNING metadata and cannot see another Base's table, so the cross-schema link
# is made with the Column object and the relationship names the class.
from .RefModels import Airline, CURRENCIES, CURRENCY_VALUES

MAX_ENGINES = 4


class AircraftCategory(PyEnum):
    """What the airframe is FOR. Three values, no fourth, no NULL.

    Derived from Cirium's `Primary Usage`, which is far finer (49 values) and per-AIRFRAME rather
    than per-type. The collapse is deliberate:

      * `passenger` — airline passengers AND business aviation, which the business treats the
        same way: `Passenger`, both `Business - *` roles, `Private Use`, `VIP / Head of State`,
        `Sightseeing / Tourist`, `Government - Liaison`. Anything that carries people for a living.
      * `cargo` — `Freight / Cargo`, plus the convertibles (`Combi / Mixed`,
        `Quick-Change/Convertible`): a cargo deck is exactly what distinguishes those from an
        ordinary passenger aircraft, so they are matched against cargo cover, not passenger cover.
      * `other` — everything that is neither, and it is a LOT: military, training, agriculture,
        EMS, police, survey, firefighting, experimental. Not a dumping ground for unknowns but a
        statement that this type carries neither passengers nor freight for hire.

    The category is part of the TYPE's identity, not a property of the airframe, because an
    Airbus A300-600 freighter and an A300-600 in passenger configuration are different things to
    insure. Both rows exist; `fleet.aircraft` points at the one matching its own airframe.
    """
    PASSENGER = "passenger"
    CARGO = "cargo"
    OTHER = "other"


_CATEGORY_ENUM = Enum(AircraftCategory, name="aircraft_category", schema="fleet",
                      values_callable=lambda e: [m.value for m in e])


class AircraftType(Base):
    """A reusable aircraft type: MANUFACTURER AND MASTER SERIES together, e.g. Airbus / A320-232.

    The key is the TRIPLE — manufacturer, master series and category. Cirium carries 806 distinct
    manufacturer/series pairs across Commercial and Business & Helicopters but only 751 distinct
    series, because 48 series are built by more than one manufacturer under licence — Kawasaki builds the BK117, Mitsubishi the CRJ family and the
    UH-60, Harbin the ERJ-145, Viking Air the DHC-6. Same design, different build, two rows.
    NULLS NOT DISTINCT so a series entered with no manufacturer still cannot be inserted twice.

    `template_url` points at the outline drawing the portal overlays damage on — a URL into the
    platform's image store, never the bytes themselves. Keeping binaries out of the row means a
    grid can read a hundred types without dragging a hundred images through the connection.
    """
    __tablename__ = "aircraft_type"

    manufacturer: Mapped[Optional[str]] = mapped_column(String, nullable=True, default=None, index=True)
    manufacturer_normalized: Mapped[Optional[str]] = mapped_column(
        String, Computed("upper(btrim(manufacturer))", persisted=True), nullable=True,
    )
    master_series: Mapped[str] = mapped_column(String, nullable=False)
    master_series_normalized: Mapped[str] = mapped_column(
        String, Computed("upper(btrim(master_series))", persisted=True), nullable=False,
    )
    category: Mapped[AircraftCategory] = mapped_column(
        _CATEGORY_ENUM, nullable=False, server_default=text("'other'"), index=True,
        comment="What the airframe is for: passenger (airline AND business aviation), cargo "
                "(freight and the convertibles), or other (military, training, EMS, utility - "
                "neither of the first two). Part of the type's identity: an A300-600 freighter "
                "and an A300-600 in passenger layout are separate rows.",
    )
    template_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True, default=None)

    __table_args__ = (
        # The CATEGORY is part of the key. Without it 'Airbus A300-600' is one row that has to be
        # either a freighter or a passenger aircraft and is wrong for half the fleet; with it the
        # pair is two rows and every aircraft points at the true one.
        UniqueConstraint(
            "manufacturer_normalized", "master_series_normalized", "category",
            name="uq_aircraft_type_manufacturer_series", postgresql_nulls_not_distinct=True,
        ),
    )


class EngineType(Base):
    """An engine model, keyed exactly as `AircraftType` is: MANUFACTURER AND MASTER SERIES.

    WHICH LEVEL OF NAME. Cirium nests engine names four deep — Engine Type (V2500), Engine Master
    Series (V2500-A5), Engine Series (V2527), Engine Sub Series (V2527-A5); for CFM that reads
    CFM56, CFM56-5, CFM56-5B, CFM56-5B3/3. This table holds the MASTER SERIES, the same granularity
    the airframe side uses, so both halves of the domain describe hardware at one level. Cirium has
    365 such pairs, 464 at series level and 952 at sub-series level; a finer column is one migration
    away if the business ever needs it.

    No engine master series in Cirium is currently built by more than one manufacturer — unlike the
    airframes, where 48 are. The key is still the pair: the reason it is a pair over there (licence
    production) applies here too, and the first collision should not need a migration to survive.
    """
    __tablename__ = "engine_type"

    manufacturer: Mapped[Optional[str]] = mapped_column(String, nullable=True, default=None, index=True)
    manufacturer_normalized: Mapped[Optional[str]] = mapped_column(
        String, Computed("upper(btrim(manufacturer))", persisted=True), nullable=True,
    )
    master_series: Mapped[str] = mapped_column(String, nullable=False)
    master_series_normalized: Mapped[str] = mapped_column(
        String, Computed("upper(btrim(master_series))", persisted=True), nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "manufacturer_normalized", "master_series_normalized",
            name="uq_engine_type_manufacturer_series", postgresql_nulls_not_distinct=True,
        ),
        {"comment": "Engine models, keyed by manufacturer AND master series exactly as "
                    "fleet.aircraft_type is. Holds Cirium's Engine Master Series level "
                    "(V2500-A5, CFM56-5), the same granularity the airframe side uses. "
                    "Loaded by _admin/load_types.py."},
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
    # 1:1. The API creates it with the aircraft, so every airframe has one; a NULL here means the
    # aircraft predates that and should be read as the defaults.
    service: Mapped[Optional["ServiceInfo"]] = relationship(
        "ServiceInfo", back_populates="aircraft", lazy="selectin", uselist=False,
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_aircraft_registration_normalized", "registration_normalized"),
        Index("uq_aircraft_msn", "msn", unique=True, postgresql_where=text("msn IS NOT NULL")),
    )


# --- the service block -------------------------------------------------------------------------
# `values_callable` stores the enum VALUE ('lease_agreement'), not the member name.

class RecordSource(PyEnum):
    """Where an aircraft's record came from."""
    MANUAL = "manual"
    LEASE_AGREEMENT = "lease_agreement"
    CIRIUM = "cirium"


class InsuranceStatus(PyEnum):
    """Whether the aircraft is covered. `not_insured` states a KNOWN gap — somebody decided this
    aircraft carries no cover — which is a different fact from an aircraft nobody has entered a
    policy for yet, and the comparison report reads it as such."""
    INSURED = "insured"
    NOT_INSURED = "not_insured"


_SOURCE_ENUM = Enum(RecordSource, name="record_source", schema="fleet",
                    values_callable=lambda e: [m.value for m in e])
_STATUS_ENUM = Enum(InsuranceStatus, name="insurance_status", schema="fleet",
                    values_callable=lambda e: [m.value for m in e])


class ServiceInfo(Base):
    """The specification's SERVICE BLOCK, one row per aircraft — bookkeeping metadata, not
    maintenance.

    These six fields used to be scattered: `agreed_value_fixed`, `source`, `status` and
    `usage_status` on the lease record, the lease currency on the agreement, the policy currency on
    the policy. They are one block about one aircraft, so they live together, next to the airframe
    they describe.

    `source` DEFAULTS TO CIRIUM — most records arrive from the feed, and a default matching the
    common case is one less field to fill in. `usage_status` is free text on purpose: Cirium owns
    that vocabulary (In Service, Storage, Retired, Written off, Type swap, Reengineered, ...) and
    adds to it, and an enum would turn each new value into a migration that blocks an import.

    WHAT HOLDING THE CURRENCIES HERE GIVES UP. They are properties of a CONTRACT — one lease
    agreement covers several aircraft in one currency, one policy likewise. Per aircraft, nothing
    stops two aircraft on the same agreement recording different currencies for it; the schema can
    no longer state that they must agree, so whoever writes them must. Moving `currency` back onto
    `leasing.agreement` / `policy.policy` would restore that and leaves the rest of this table
    alone.
    """
    __tablename__ = "service_info"

    aircraft_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("fleet.aircraft.id", ondelete="CASCADE"), nullable=False,
    )
    agreed_value_fixed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"),
    )
    source: Mapped[RecordSource] = mapped_column(
        _SOURCE_ENUM, nullable=False, server_default=text("'cirium'"), index=True,
        comment="Where this aircraft's record came from. Defaults to cirium - most arrive from "
                "the feed.",
    )
    status: Mapped[InsuranceStatus] = mapped_column(
        _STATUS_ENUM, nullable=False, server_default=text("'insured'"), index=True,
        comment="Whether the aircraft is covered. not_insured states a KNOWN gap, which is "
                "different from an aircraft nobody has entered a policy for - "
                "/policy/coverage/compare reads it so a deliberate gap is not reported as a "
                "mistake.",
    )
    usage_status: Mapped[Optional[str]] = mapped_column(
        String, nullable=True, default=None,
        comment="The airframe's operational status as Cirium states it (In Service, Storage, "
                "Retired, Written off, Type swap, ...). Text, not an enum: Cirium owns the "
                "vocabulary and adds to it.",
    )
    lease_currency: Mapped[str] = mapped_column(
        String(3), nullable=False, server_default=text("'USD'"),
        comment="The lease agreement's currency. Held per aircraft since revision "
                "service_info_table, so nothing stops two aircraft on one agreement disagreeing - "
                "whoever writes them must keep them consistent.",
    )
    policy_currency: Mapped[str] = mapped_column(
        String(3), nullable=False, server_default=text("'USD'"),
        comment="The policy's currency. Held per aircraft since revision service_info_table, so "
                "nothing stops two aircraft on one policy disagreeing - whoever writes them must "
                "keep them consistent.",
    )

    aircraft: Mapped["Aircraft"] = relationship("Aircraft", back_populates="service")

    __table_args__ = (
        UniqueConstraint("aircraft_id", name="uq_service_info_aircraft"),
        CheckConstraint(
            f"lease_currency IN ({CURRENCY_VALUES}) AND lease_currency = upper(lease_currency)",
            name="ck_service_info_lease_currency"),
        CheckConstraint(
            f"policy_currency IN ({CURRENCY_VALUES}) AND policy_currency = upper(policy_currency)",
            name="ck_service_info_policy_currency"),
        {"comment": "The specification's service block, one row per aircraft: how the record came "
                    "to be (source), whether it is covered (status), what Cirium says the airframe "
                    "is doing (usage_status), whether the agreed value depreciates, and the two "
                    "contract currencies. Bookkeeping metadata, NOT maintenance."},
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

    THE MODEL IS A REFERENCE, not text on the row: `engine_type_id` points at `fleet.engine_type`,
    the same way an airframe points at `fleet.aircraft_type`. What stays on the installation is what
    is true of THIS engine and no other — its serial, when it went on, and the note.
    """
    __tablename__ = "aircraft_engine"

    aircraft_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("fleet.aircraft.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    engine_type_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("fleet.engine_type.id", ondelete="RESTRICT"),
        index=True, nullable=True, default=None,
    )
    msn: Mapped[Optional[str]] = mapped_column(String, nullable=True, default=None, index=True)
    installed_on: Mapped[Optional[date]] = mapped_column(Date, nullable=True, default=None)
    details: Mapped[Optional[str]] = mapped_column(Text, nullable=True, default=None)

    aircraft: Mapped["Aircraft"] = relationship("Aircraft", back_populates="engines")
    engine_type: Mapped[Optional["EngineType"]] = relationship("EngineType", lazy="selectin")

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
