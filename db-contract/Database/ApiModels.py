import inspect
import sys
from datetime import datetime, date
from decimal import Decimal
from enum import Enum as PyEnum
from typing import Optional, List

from sqlalchemy import (
    String, BigInteger, ForeignKey, Integer, Float, Boolean, Date, DateTime,
    Numeric, Enum, Computed, UniqueConstraint, CheckConstraint, Index, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .config import ApiBase as Base


# --- Enumerations (native PG enums in schema `api`) -------------------------------------------
# `values_callable` makes SQLAlchemy store the enum VALUE ('insured'), not the member NAME
# ('INSURED'). These classes are the single definition — the API layer imports them from
# `Database` rather than re-declaring a parallel copy in Schemas/Enums.

class InsuranceStatus(str, PyEnum):
    """Whether the aircraft is covered over the record's effective period."""
    INSURED = "insured"
    NOT_INSURED = "not_insured"


class InsuranceSource(str, PyEnum):
    """Provenance of the row: parsed from a lease agreement, pulled from Cirium, or hand-entered."""
    LEASE_AGR = "lease_agr"
    CIRIUM = "cirium"
    MANUAL = "manual"


class ClaimDamageType(str, PyEnum):
    """Which section of the cover a claim falls under. The three values line up 1:1 with the
    reserve/paid column pairs on api.insurance_claims (hd_* / hw_* / hsl_*)."""
    HULL_DEDUCTIBLE = "hull_deductible"    # HD
    HULL_WAR = "hull_war"                  # HW
    HULL_SPARES = "hull_spares"            # HSL — hull & spares


_STATUS_ENUM = Enum(
    InsuranceStatus, name="insurance_status", schema="api",
    values_callable=lambda e: [m.value for m in e],
)
_SOURCE_ENUM = Enum(
    InsuranceSource, name="insurance_source", schema="api",
    values_callable=lambda e: [m.value for m in e],
)
_DAMAGE_ENUM = Enum(
    ClaimDamageType, name="claim_damage_type", schema="api",
    values_callable=lambda e: [m.value for m in e],
)


class Airlines(Base):
    airline_name: Mapped[str] = mapped_column(String, index=True)
    icao: Mapped[str] = mapped_column(String, index=True, nullable=True, default=None)
    iata: Mapped[str] = mapped_column(String, index=True, nullable=True, default=None)


# Active aircraft taken from cirium.asg (is_active = true). NOT hand-maintained: the table is
# rebuilt by the DB function api.sync_registration_from_asg() after every cirium.asg refresh
# (external-worker calls it right after the asg matview REFRESH). `airline` resolves the airline
# name matched in asg to the api.airlines row.
#
# WARNING: that function does TRUNCATE ... RESTART IDENTITY, so this table's `id` is NOT stable.
# NEVER point a foreign key at it — the insurance tables anchor on `api.aircrafts` instead and
# join to this one on reg/msn only.
class Registration(Base):
    reg: Mapped[str] = mapped_column(String, index=True)                 # Registration
    msn: Mapped[str] = mapped_column(String, index=True, nullable=True, default=None)  # Serial Number
    airline_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("api.airlines.id", ondelete="SET NULL"),
        index=True, nullable=True, default=None,
    )
    airline: Mapped["Airlines"] = relationship("Airlines", lazy="selectin")


# ==============================================================================================
# Insurance domain
# ==============================================================================================
# Three layers:
#   1. reference data reused across records — parties / aircraft_types / engine_types (+ airlines)
#   2. the airframe and its OPTIONAL technical data — aircrafts / aircraft_specs / aircraft_engines
#   3. insurance itself — insurance_policies (the contract) / insurance_records (one time-bounded
#      record per aircraft) / insurance_record_history (audit trail written by a DB trigger)
#   4. claims — insurance_claims (one loss event) / insurance_claim_history (its audit trail)
#
# History is kept on two levels: business history comes from the effective periods (a renewal is a
# new policy + new records, the old ones stay), technical history from the audit trigger (mid-term
# endorsements and corrections to a record that is already in force).
# ==============================================================================================


class Parties(Base):
    """Counterparties — every non-airline legal entity the insurance domain names: lessees, lessors,
    lead underwriters and surveyors, all in ONE table, because the same entity turns up in several
    roles (a lessor on one aircraft is a lessee on another under a sub-lease). The role is decided by
    the referencing column (`lessee_id` / `lessor_id` / `surveyor_id` / `leader_id`); the `is_*` flags
    are cumulative UI hints for autocomplete, not constraints.

    `name_normalized` is a STORED generated column (upper + trimmed) carrying the unique constraint,
    so importers cannot create 'AerCap' / 'AERCAP ' duplicates.
    """
    __tablename__ = "parties"

    name: Mapped[str] = mapped_column(String, nullable=False)
    name_normalized: Mapped[str] = mapped_column(
        String, Computed("upper(btrim(name))", persisted=True), nullable=False,
    )
    country: Mapped[Optional[str]] = mapped_column(String, nullable=True, default=None)
    is_lessor: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    is_lessee: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # claims side: the lead underwriter on a claim, and the surveyor / loss adjuster appointed to it
    is_insurer: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    is_surveyor: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    __table_args__ = (
        UniqueConstraint("name_normalized", name="uq_parties_name_normalized"),
    )


class EngineTypes(Base):
    """Reusable engine model, e.g. 'CFM56-5B4/3'. Referenced per installed engine."""
    __tablename__ = "engine_types"

    name: Mapped[str] = mapped_column(String, nullable=False)
    name_normalized: Mapped[str] = mapped_column(
        String, Computed("upper(btrim(name))", persisted=True), nullable=False,
    )
    manufacturer: Mapped[Optional[str]] = mapped_column(String, nullable=True, default=None)

    __table_args__ = (
        UniqueConstraint("name_normalized", name="uq_engine_types_name_normalized"),
    )


class AircraftTypes(Base):
    """Reusable aircraft type, e.g. 'A320-232'. The `default_*` columns are fallbacks used by the
    importer when the incoming row carries no technical data — they are NOT the aircraft's truth,
    api.aircraft_specs is."""
    __tablename__ = "aircraft_types"

    name: Mapped[str] = mapped_column(String, nullable=False)
    name_normalized: Mapped[str] = mapped_column(
        String, Computed("upper(btrim(name))", persisted=True), nullable=False,
    )
    manufacturer: Mapped[Optional[str]] = mapped_column(String, nullable=True, default=None)
    default_mtow_kg: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True, default=None)
    default_number_of_engines: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=None)
    default_engine_type_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("api.engine_types.id", ondelete="SET NULL"),
        index=True, nullable=True, default=None,
    )

    default_engine_type: Mapped[Optional["EngineTypes"]] = relationship("EngineTypes", lazy="selectin")

    __table_args__ = (
        UniqueConstraint("name_normalized", name="uq_aircraft_types_name_normalized"),
    )


class Aircrafts(Base):
    """The airframe — the stable anchor everything else hangs off. Distinct from api.registration,
    which is a rebuilt-from-scratch projection of cirium.asg (see the WARNING there).

    Identity is MSN, not the registration: a tail number changes when the aircraft changes operator
    or jurisdiction, the manufacturer serial never does. Hence UNIQUE on msn (partial: rows with an
    unknown msn are still allowed) and only an index on registration.

    `registration_normalized` (upper, separators stripped) makes lookups separator-insensitive —
    'YLLTD' finds 'YL-LTD' — matching the convention in Routers/Registrations.py.
    """
    __tablename__ = "aircrafts"

    registration: Mapped[str] = mapped_column(String, nullable=False, index=True)
    registration_normalized: Mapped[str] = mapped_column(
        String,
        Computed("upper(regexp_replace(registration, '[^A-Za-z0-9]', '', 'g'))", persisted=True),
        nullable=False,
    )
    msn: Mapped[Optional[str]] = mapped_column(String, nullable=True, default=None, index=True)
    aircraft_type_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("api.aircraft_types.id", ondelete="SET NULL"),
        index=True, nullable=True, default=None,
    )
    airline_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("api.airlines.id", ondelete="SET NULL"),
        index=True, nullable=True, default=None,
    )

    aircraft_type: Mapped[Optional["AircraftTypes"]] = relationship("AircraftTypes", lazy="selectin")
    airline: Mapped[Optional["Airlines"]] = relationship("Airlines", lazy="selectin")
    specs: Mapped[Optional["AircraftSpecs"]] = relationship(
        "AircraftSpecs", back_populates="aircraft", lazy="selectin", uselist=False,
        cascade="all, delete-orphan",
    )
    engines: Mapped[List["AircraftEngines"]] = relationship(
        "AircraftEngines", back_populates="aircraft", lazy="selectin",
        order_by="AircraftEngines.position", cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_aircrafts_registration_normalized", "registration_normalized"),
        Index("uq_aircrafts_msn", "msn", unique=True, postgresql_where=text("msn IS NOT NULL")),
    )


class AircraftSpecs(Base):
    """Technical data — a SEPARATE 1:1 table because it is not always known (an insurance row can
    arrive with nothing but registration + values). A LEFT JOIN that yields NULL then means
    'we have no technical data', which a nullable column on api.aircrafts could not distinguish
    from 'known to be empty'.

    `source` is tracked here on its own: MTOW/engine count typically comes from Cirium even when the
    insurance record itself was typed in by hand.
    """
    __tablename__ = "aircraft_specs"

    aircraft_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("api.aircrafts.id", ondelete="CASCADE"), nullable=False,
    )
    mtow_kg: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True, default=None)
    number_of_engines: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=None)
    source: Mapped[Optional[InsuranceSource]] = mapped_column(_SOURCE_ENUM, nullable=True, default=None)

    aircraft: Mapped["Aircrafts"] = relationship("Aircrafts", back_populates="specs")

    __table_args__ = (
        UniqueConstraint("aircraft_id", name="uq_aircraft_specs_aircraft_id"),
        CheckConstraint("mtow_kg IS NULL OR mtow_kg > 0", name="ck_aircraft_specs_mtow_positive"),
        CheckConstraint(
            "number_of_engines IS NULL OR number_of_engines BETWEEN 1 AND 8",
            name="ck_aircraft_specs_engine_count",
        ),
    )


class AircraftEngines(Base):
    """One row per installed engine, replacing the flat engine_msn_1..4 columns.

    Engines are separately-serialised assets: they get swapped between airframes, an aircraft can
    fly with mixed sub-variants, and 4 columns is a hard ceiling. `installed_from` / `installed_to`
    give the swap history for free; the currently-fitted set is `installed_to IS NULL`, which is
    also what the partial unique index on (aircraft_id, position) guards.
    """
    __tablename__ = "aircraft_engines"

    aircraft_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("api.aircrafts.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    engine_msn: Mapped[Optional[str]] = mapped_column(String, nullable=True, default=None, index=True)
    engine_type_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("api.engine_types.id", ondelete="SET NULL"),
        index=True, nullable=True, default=None,
    )
    installed_from: Mapped[Optional[date]] = mapped_column(Date, nullable=True, default=None)
    installed_to: Mapped[Optional[date]] = mapped_column(Date, nullable=True, default=None)

    aircraft: Mapped["Aircrafts"] = relationship("Aircrafts", back_populates="engines")
    engine_type: Mapped[Optional["EngineTypes"]] = relationship("EngineTypes", lazy="selectin")

    __table_args__ = (
        CheckConstraint("position BETWEEN 1 AND 8", name="ck_aircraft_engines_position"),
        CheckConstraint(
            "installed_to IS NULL OR installed_from IS NULL OR installed_to >= installed_from",
            name="ck_aircraft_engines_period",
        ),
        Index(
            "uq_aircraft_engines_current_position", "aircraft_id", "position",
            unique=True, postgresql_where=text("installed_to IS NULL"),
        ),
    )


class InsurancePolicies(Base):
    """The insurance contract itself — the part shared by every aircraft on it: the insured airline,
    the period, the currency and the (policy-level) combined single limit.

    Natural key = (airline_id, policy_from, policy_to) so a flat import row can find-or-create its
    policy without a policy number; declared NULLS NOT DISTINCT so an open-ended policy_to still
    collides with itself instead of inserting duplicates.
    """
    __tablename__ = "insurance_policies"

    airline_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("api.airlines.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    policy_number: Mapped[Optional[str]] = mapped_column(String, nullable=True, default=None, index=True)
    policy_from: Mapped[date] = mapped_column(Date, nullable=False)
    policy_to: Mapped[Optional[date]] = mapped_column(Date, nullable=True, default=None)
    # Single currency per policy. Always USD today; the column exists so a non-USD policy does not
    # need a migration, and so no amount is ever ambiguous.
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default=text("'USD'"))
    combined_single_limit: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True, default=None)

    airline: Mapped["Airlines"] = relationship("Airlines", lazy="selectin")
    records: Mapped[List["InsuranceRecords"]] = relationship(
        "InsuranceRecords", back_populates="policy", lazy="selectin",
    )

    __table_args__ = (
        UniqueConstraint(
            "airline_id", "policy_from", "policy_to",
            name="uq_insurance_policies_airline_period",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(
            "policy_to IS NULL OR policy_to >= policy_from", name="ck_insurance_policies_period",
        ),
        CheckConstraint("currency = upper(currency)", name="ck_insurance_policies_currency_upper"),
    )


class InsuranceRecords(Base):
    """One time-bounded statement about ONE aircraft's insurance — the flat source row, normalised.

    `policy_id` is nullable on purpose: a `not_insured` record states that the aircraft is knowingly
    uncovered over that period and has no contract behind it. A check constraint keeps the pairing
    honest (status = 'insured' requires a policy).

    `combined_single_limit` here is an OVERRIDE — read it as
    COALESCE(record.combined_single_limit, policy.combined_single_limit); most schedules set the CSL
    once per policy and only some vary it per aircraft.

    The GiST exclusion constraint enforces the real-world invariant: an aircraft has exactly ONE
    known insurance state on any given day. It needs the btree_gist extension (created in the
    migration). If the business later layers policies (hull + war risk simultaneously), drop
    `ex_insurance_records_no_overlap` — nothing else depends on it.

    Updates are non-destructive: a DB trigger copies the pre-image into
    api.insurance_record_history on every UPDATE/DELETE.
    """
    __tablename__ = "insurance_records"

    aircraft_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("api.aircrafts.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    policy_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("api.insurance_policies.id", ondelete="CASCADE"),
        nullable=True, default=None, index=True,
    )

    # --- validity of THIS record. For an insured row these mirror the policy period, but they are
    # held separately so an aircraft can join or leave a policy mid-term (delivery / redelivery).
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[Optional[date]] = mapped_column(Date, nullable=True, default=None)

    status: Mapped[InsuranceStatus] = mapped_column(
        _STATUS_ENUM, nullable=False, server_default=text("'insured'"), index=True,
    )
    source: Mapped[InsuranceSource] = mapped_column(_SOURCE_ENUM, nullable=False)

    # --- parties to the lease. Same table both sides; the column decides the role.
    lessee_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("api.parties.id", ondelete="SET NULL"),
        nullable=True, default=None, index=True,
    )
    lessor_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("api.parties.id", ondelete="SET NULL"),
        nullable=True, default=None, index=True,
    )

    # --- money. Numeric everywhere; the unit is the parent policy's `currency`.
    hull_deductible: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True, default=None)
    hull_spares_deductible: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True, default=None)
    combined_single_limit: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True, default=None)
    agreed_value_inception: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True, default=None)
    agreed_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True, default=None)
    agreed_value_fixed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"),
    )

    # --- depreciation. `depreciation_rate` is a FRACTION per annum (0.05 = 5 % p.a.), not a percent.
    depreciation_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True, default=None)
    depreciation_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 4), nullable=True, default=None)

    aircraft: Mapped["Aircrafts"] = relationship("Aircrafts", lazy="selectin")
    policy: Mapped[Optional["InsurancePolicies"]] = relationship(
        "InsurancePolicies", back_populates="records", lazy="selectin",
    )
    lessee: Mapped[Optional["Parties"]] = relationship(
        "Parties", foreign_keys=[lessee_id], lazy="selectin",
    )
    lessor: Mapped[Optional["Parties"]] = relationship(
        "Parties", foreign_keys=[lessor_id], lazy="selectin",
    )

    __table_args__ = (
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_insurance_records_period",
        ),
        CheckConstraint(
            "status <> 'insured' OR policy_id IS NOT NULL",
            name="ck_insurance_records_insured_needs_policy",
        ),
        CheckConstraint(
            "depreciation_rate IS NULL OR depreciation_rate BETWEEN 0 AND 1",
            name="ck_insurance_records_depreciation_rate",
        ),
        Index("ix_insurance_records_effective", "effective_from", "effective_to"),
    )


class InsuranceRecordHistory(Base):
    """Audit trail of api.insurance_records, written by the AFTER UPDATE/DELETE trigger
    api.insurance_records_audit(). Deliberately NOT a versioned-rows design: reads of the current
    state stay a plain SELECT with no `WHERE is_current`, and corrections/endorsements are rare
    compared with renewals (which are already modelled as new policies + new records).

    `record_id` carries NO foreign key on purpose — the audit row must outlive a deleted record.
    `changed_by` reads the `app.actor` GUC when the API sets it (SET LOCAL app.actor = '...'),
    falling back to the database session user.
    """
    __tablename__ = "insurance_record_history"

    record_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    operation: Mapped[str] = mapped_column(String(10), nullable=False)  # UPDATE | DELETE
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"), index=True,
    )
    changed_by: Mapped[Optional[str]] = mapped_column(String, nullable=True, default=None)
    old_row: Mapped[dict] = mapped_column(JSONB, nullable=False)
    new_row: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True, default=None)


# ==============================================================================================
# Claims — insurance events against an aircraft (damage, war risk, total loss)
# ==============================================================================================


class InsuranceClaims(Base):
    """One claim = one loss event on one aircraft. Anchored on api.aircrafts (the stable airframe),
    linked to the policy that was in force — that link IS the source column `policy_period`, so the
    period never has to be typed twice or kept in sync by hand.

    Deliberately FLAT on the money: `indemnity_reserve` / `paid_amount` are the claim totals as the
    schedule states them, and hd_/hw_/hsl_ are the per-section breakdown matching the three
    ClaimDamageType values (Hull Deductible / Hull War / Hull & Spares). A child table would have
    been the tidier normal form, but the set of sections is closed and fixed, and — the deciding
    reason — a single row means a SINGLE audit trigger captures every money change. The change
    history is the point of this table; splitting the amounts out would split their history too.

    The totals are NOT derived from the breakdown: the schedule supplies both and they do not always
    reconcile. Store what was stated; compare in the read layer, never silently overwrite.

    Nothing here is unique per aircraft+date: one aircraft can suffer several distinct losses on the
    same day, so there is no exclusion constraint (unlike api.insurance_records). `claim_reference`
    is the only uniqueness, and only when supplied.
    """
    __tablename__ = "insurance_claims"

    aircraft_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("api.aircrafts.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    # the operator AT THE TIME OF LOSS — held on the claim, not read through the aircraft, because
    # the aircraft's current operator may have changed since.
    airline_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("api.airlines.id", ondelete="SET NULL"),
        nullable=True, default=None, index=True,
    )
    # RESTRICT, not CASCADE: a claim must never disappear because its policy row was deleted.
    policy_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("api.insurance_policies.id", ondelete="RESTRICT"),
        nullable=True, default=None, index=True,
    )
    claim_reference: Mapped[Optional[str]] = mapped_column(String, nullable=True, default=None)

    type_of_damage: Mapped[ClaimDamageType] = mapped_column(_DAMAGE_ENUM, nullable=False, index=True)
    date_of_loss: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    location_of_loss: Mapped[Optional[str]] = mapped_column(String, nullable=True, default=None)
    damage: Mapped[Optional[str]] = mapped_column(String, nullable=True, default=None)

    # --- who is handling it. Both point at api.parties; the column decides the role.
    surveyor_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("api.parties.id", ondelete="SET NULL"),
        nullable=True, default=None, index=True,
    )
    leader_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("api.parties.id", ondelete="SET NULL"),
        nullable=True, default=None, index=True,
    )

    # --- money. Own currency column: a claim can be settled in a currency other than the policy's.
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default=text("'USD'"))
    indemnity_reserve: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True, default=None)
    paid_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True, default=None)
    paid_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True, default=None)

    # --- per-section breakdown: HD = hull deductible, HW = hull war, HSL = hull & spares
    hd_reserve: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True, default=None)
    hd_paid: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True, default=None)
    hw_reserve: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True, default=None)
    hw_paid: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True, default=None)
    hsl_reserve: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True, default=None)
    hsl_paid: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True, default=None)

    aircraft: Mapped["Aircrafts"] = relationship("Aircrafts", lazy="selectin")
    airline: Mapped[Optional["Airlines"]] = relationship("Airlines", lazy="selectin")
    policy: Mapped[Optional["InsurancePolicies"]] = relationship("InsurancePolicies", lazy="selectin")
    surveyor: Mapped[Optional["Parties"]] = relationship(
        "Parties", foreign_keys=[surveyor_id], lazy="selectin",
    )
    leader: Mapped[Optional["Parties"]] = relationship(
        "Parties", foreign_keys=[leader_id], lazy="selectin",
    )

    __table_args__ = (
        CheckConstraint(
            "paid_date IS NULL OR paid_date >= date_of_loss", name="ck_insurance_claims_paid_date",
        ),
        # LEAST() ignores NULLs and is NULL only when every argument is — so unknown amounts pass
        # and any stated negative amount fails.
        CheckConstraint(
            "LEAST(indemnity_reserve, paid_amount, hd_reserve, hd_paid, hw_reserve, hw_paid,"
            " hsl_reserve, hsl_paid) >= 0",
            name="ck_insurance_claims_amounts_non_negative",
        ),
        CheckConstraint("currency = upper(currency)", name="ck_insurance_claims_currency_upper"),
        Index(
            "uq_insurance_claims_reference", "claim_reference",
            unique=True, postgresql_where=text("claim_reference IS NOT NULL"),
        ),
        Index("ix_insurance_claims_aircraft_date", "aircraft_id", "date_of_loss"),
    )


class InsuranceClaimHistory(Base):
    """Audit trail of api.insurance_claims, written by api.insurance_claims_audit().

    Unlike api.insurance_record_history this also captures INSERT (hence the nullable `old_row`):
    the UI renders a full timeline for a claim — who opened it and every reserve/payment movement
    since — and a create event with its actor is part of that story.

    `claim_id` carries NO foreign key on purpose: the audit row must outlive a deleted claim.
    """
    __tablename__ = "insurance_claim_history"

    claim_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    operation: Mapped[str] = mapped_column(String(10), nullable=False)  # INSERT | UPDATE | DELETE
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"), index=True,
    )
    changed_by: Mapped[Optional[str]] = mapped_column(String, nullable=True, default=None)
    old_row: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True, default=None)
    new_row: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True, default=None)


_current_module = sys.modules[__name__]

__all__ = [
    name
    for name, obj in globals().items()
    if inspect.isclass(obj) and obj.__module__ == __name__
]
