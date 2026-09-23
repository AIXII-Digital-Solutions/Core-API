"""Lease agreements and their per-aircraft terms — schema `leasing` in the aixii database.

What the LESSOR REQUIRES. Its twin is schema `policy`, which holds what the insurer actually
PROVIDES; the two carry three deliberately identical columns — `combined_single_limit`,
`hull_spares_war_excess_liability`, `hull_deductible_buy_down` — so that "is this aircraft insured
to the standard its lease demands" is a comparison between two rows and not a matter of memory.
Never collapse them into one column.

Two levels, for the same reason policies have two: an agreement covers a fleet, the terms are per
aircraft.

    leasing.agreement       the contract — name, start date, lessor, currency, the parties and
                            documents it names
    leasing.aircraft_lease  one aircraft under that agreement from a given effective date —
                            agreed values, depreciation, the limits stipulated

Alembic reads THIS file (db-contract); `app/Database/LeasingModels.py` is core-api's runtime copy.
"""
import inspect
import sys
from datetime import date
from decimal import Decimal
from typing import Optional, List

from sqlalchemy import (
    String, Text, BigInteger, Numeric, Date, ForeignKey, Computed,
    UniqueConstraint, CheckConstraint, Index, text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .config import LeasingBase as Base
# A ForeignKey STRING is resolved inside the OWNING MetaData, which cannot see another
# Base's tables, so every CROSS-SCHEMA link below is made with the Column object.
from .RefModels import Party
from .FleetModels import Aircraft


class Agreement(Base):
    """The lease agreement — the part shared by every aircraft on it.

    Natural key is (name, start_date), declared NULLS NOT DISTINCT so an agreement with no start
    date still collides with itself instead of being inserted twice by a find-or-create write path.

    The agreement's CURRENCY is not here: it moved to `fleet.service_info.lease_currency` with the
    rest of the service block (revision `service_info_table`), which means it is recorded per
    aircraft rather than per contract — see that model for what the move gives up.

    `alternative_contract_party` and `other_contracts` are free text on purpose: they quote clauses
    ("… and any novation thereof"), not entities, and forcing them into a reference table would
    mean inventing parties that do not exist as counterparties anywhere else.
    """
    __tablename__ = "agreement"

    name: Mapped[str] = mapped_column(String, nullable=False)
    name_normalized: Mapped[str] = mapped_column(
        String, Computed("upper(btrim(name))", persisted=True), nullable=False,
    )
    start_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True, default=None)
    lessor_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey(Party.__table__.c.id, ondelete="RESTRICT"),
        nullable=True, default=None, index=True,
    )
    alternative_contract_party: Mapped[Optional[str]] = mapped_column(Text, nullable=True, default=None)
    other_contracts: Mapped[Optional[str]] = mapped_column(Text, nullable=True, default=None)

    lessor: Mapped[Optional["Party"]] = relationship(Party, lazy="raise_on_sql")
    aircraft_leases: Mapped[List["AircraftLease"]] = relationship(
        "AircraftLease", back_populates="agreement", lazy="raise_on_sql",
    )

    __table_args__ = (
        UniqueConstraint(
            "name_normalized", "start_date", name="uq_agreement_name_start",
            postgresql_nulls_not_distinct=True,
        ),
    )


class AircraftLease(Base):
    """One aircraft under one agreement, from `effective_date`.

    There is no end date, by design: a change of terms is a NEW row with a later effective date,
    and the terms in force on any day are the newest row not later than that day —

        SELECT DISTINCT ON (aircraft_id) *
        FROM leasing.aircraft_lease
        WHERE aircraft_id = ? AND effective_date <= ?
        ORDER BY aircraft_id, effective_date DESC, id DESC

    That keeps the business history (what was agreed, and when it changed) separate from the audit
    history in `audit.change_log` (a correction to a row that was already written). Both exist and
    they answer different questions.

    AGREED VALUE. `agreed_value_preliminary` is the value at inception. `agreed_value_final` is
    what the schedule STATES as the depreciated value — stored rather than computed, because the
    stated figure and the formula do not always reconcile and the stated one is what the contract
    says. `leasing.agreed_value_at(...)` recomputes it from the same inputs so the read layer can
    show both and flag a divergence; it never silently overwrites.

    `depreciation_ratio` is a PERCENT per annum (5.0 = 5 %/year), not a fraction — the spec and the
    portal both speak percent, so the column does too. Whether the value depreciates AT ALL is
    `fleet.service_info.agreed_value_fixed`, which lives with the rest of the service block.

    The three limit columns mirror `policy.policy` exactly (see this module's docstring).

    """
    __tablename__ = "aircraft_lease"

    aircraft_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey(Aircraft.__table__.c.id, ondelete="RESTRICT"), nullable=False,
        # Two composites already lead with it (uq_aircraft_lease_effective and
        # ix_aircraft_lease_aircraft_effective); a third, narrower copy earns nothing.
        index=False,
    )
    agreement_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("leasing.agreement.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)

    # --- agreed value and its depreciation
    agreed_value_preliminary: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True, default=None)
    agreed_value_final: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True, default=None)
    depreciation_ratio: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 3), nullable=True, default=None)
    depreciation_start_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True, default=None)

    # --- the cover the lease stipulates. Same three columns exist on policy.policy.
    combined_single_limit: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True, default=None)
    hull_spares_war_excess_liability: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True, default=None)
    hull_deductible_buy_down: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True, default=None)


    agreement: Mapped["Agreement"] = relationship(
        "Agreement", back_populates="aircraft_leases", lazy="raise_on_sql",
    )

    __table_args__ = (
        UniqueConstraint(
            "aircraft_id", "agreement_id", "effective_date", name="uq_aircraft_lease_effective",
        ),
        CheckConstraint(
            "depreciation_ratio IS NULL OR depreciation_ratio BETWEEN 0 AND 100",
            name="ck_aircraft_lease_depreciation_ratio",
        ),
        # LEAST() ignores NULLs and is NULL only when every argument is, so unknown amounts pass
        # and any stated negative amount fails.
        CheckConstraint(
            "LEAST(agreed_value_preliminary, agreed_value_final, combined_single_limit,"
            " hull_spares_war_excess_liability, hull_deductible_buy_down) >= 0",
            name="ck_aircraft_lease_amounts_non_negative",
        ),
        Index("ix_aircraft_lease_aircraft_effective", "aircraft_id", "effective_date"),
    )


_current_module = sys.modules[__name__]

__all__ = [
    name
    for name, obj in globals().items()
    if inspect.isclass(obj) and obj.__module__ == __name__
]
