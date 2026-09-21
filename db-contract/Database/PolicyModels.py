"""Insurance policies and per-aircraft coverage — schema `policy` in the aixii database.

What the insurer PROVIDES. Its twin is schema `leasing`, which holds what the lessor REQUIRES;
`combined_single_limit`, `hull_spares_war_excess_liability` and `hull_deductible_buy_down` exist on
both sides on purpose, so the two can be compared. Never collapse them.

Two levels:

    policy.policy     the contract — one row per (insured, period). Every limit and deductible
                      lives here, once, because they are written for the fleet and not per tail.
    policy.coverage   one row per aircraft per policy: which airframe that contract covers, and
                      over which window inside the policy period.

POLICY HISTORY IS THE POINT OF THIS SHAPE. An aircraft stays insured for years while the policy
behind it is renewed annually. A renewal is a NEW `policy` row and a NEW `coverage` row — the old
ones keep their periods and are never touched — so an aircraft's insurance history is simply its
coverage rows ordered by date, each pointing at the contract that was in force. That is BUSINESS
history and it is different from the technical audit trail in `audit.change_log`, which records
corrections and endorsements made to a row that already exists. Both are kept; asking "what was in
force in 2025" is the first, "who changed this deductible and when" is the second.

`insured` / `reinsured` / `retrocedent` are `ref.party`, not `api.airlines` — the insured on an
aviation policy is routinely a lessor, a holding company or a group entity rather than the
operating airline, and the reinsurance chain is never an airline at all. The operating airline is
reachable through `policy.coverage -> fleet.aircraft.airline_id`.

Alembic reads THIS file (db-contract); `app/Database/PolicyModels.py` is core-api's runtime copy.
"""
import inspect
import sys
from datetime import date
from decimal import Decimal
from typing import Optional, List

from sqlalchemy import (
    String, Text, BigInteger, Numeric, Date, ForeignKey,
    UniqueConstraint, CheckConstraint, Index, text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .config import PolicyBase as Base
# A ForeignKey STRING is resolved inside the OWNING MetaData, which cannot see another
# Base's tables, so every CROSS-SCHEMA link below is made with the Column object.
from .RefModels import Party
from .FleetModels import Aircraft


class Policy(Base):
    """One insurance contract over one period.

    Natural key (insured_id, period_from, period_to) with NULLS NOT DISTINCT, so an open-ended
    policy collides with itself instead of being inserted twice by a find-or-create write path.
    There is no policy number column: the source schedules identify a policy by insured and period,
    and a number that is only sometimes present cannot carry the identity.

    THE WAR COLUMNS. `hull_war_confiscation_limit` is the general confiscation limit;
    `hull_war_confiscation_limit_selected_country` is the (usually much lower) limit that applies
    to flights into the one territory named in `selected_country`, e.g. Russia. Both are limits on
    the same peril, which is why they sit side by side rather than in separate rows.

    The policy's CURRENCY is not here: it moved to `fleet.service_info.policy_currency` with the
    rest of the service block (revision `service_info_table`), so it is recorded per aircraft
    rather than per contract — see that model for what the move gives up.

    `cut_through_clause` is the clause text itself — it is quoted in full in correspondence, and
    what matters is the wording, not a flag saying one exists.
    """
    __tablename__ = "policy"

    insured_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey(Party.__table__.c.id, ondelete="RESTRICT"), nullable=False, index=True,
    )
    reinsured_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey(Party.__table__.c.id, ondelete="RESTRICT"),
        nullable=True, default=None, index=True,
    )
    retrocedent_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey(Party.__table__.c.id, ondelete="RESTRICT"),
        nullable=True, default=None, index=True,
    )

    period_from: Mapped[date] = mapped_column(Date, nullable=False)
    period_to: Mapped[Optional[date]] = mapped_column(Date, nullable=True, default=None)

    # --- deductibles
    hull_all_risks_deductible: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True, default=None)
    spares_deductible: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True, default=None)
    hull_deductible_buy_down: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True, default=None)
    hull_deductible_aggregate: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True, default=None)

    # --- limits
    combined_single_limit: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True, default=None)
    hull_war_overall_limit: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True, default=None)
    hull_spares_limit: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True, default=None)
    hull_spares_war_excess_liability: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True, default=None)
    hull_war_confiscation_limit: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True, default=None)
    hull_war_confiscation_limit_selected_country: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(18, 2), nullable=True, default=None,
    )
    selected_country: Mapped[Optional[str]] = mapped_column(String, nullable=True, default=None)

    # --- reinsurance. A PERCENT of the risk ceded (97.5 = 97.5 %), not a fraction.
    reinsured_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 3), nullable=True, default=None)

    cut_through_clause: Mapped[Optional[str]] = mapped_column(Text, nullable=True, default=None)

    insured: Mapped["Party"] = relationship(Party, foreign_keys=[insured_id], lazy="selectin")
    reinsured: Mapped[Optional["Party"]] = relationship(Party, foreign_keys=[reinsured_id], lazy="selectin")
    retrocedent: Mapped[Optional["Party"]] = relationship(Party, foreign_keys=[retrocedent_id], lazy="selectin")
    coverages: Mapped[List["Coverage"]] = relationship(
        "Coverage", back_populates="policy", lazy="selectin",
    )

    __table_args__ = (
        UniqueConstraint(
            "insured_id", "period_from", "period_to", name="uq_policy_insured_period",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint("period_to IS NULL OR period_to >= period_from", name="ck_policy_period"),
        CheckConstraint(
            "reinsured_amount IS NULL OR reinsured_amount BETWEEN 0 AND 100",
            name="ck_policy_reinsured_amount",
        ),
        CheckConstraint(
            "LEAST(hull_all_risks_deductible, spares_deductible, hull_deductible_buy_down,"
            " hull_deductible_aggregate, combined_single_limit, hull_war_overall_limit,"
            " hull_spares_limit, hull_spares_war_excess_liability, hull_war_confiscation_limit,"
            " hull_war_confiscation_limit_selected_country) >= 0",
            name="ck_policy_amounts_non_negative",
        ),
        Index("ix_policy_period", "period_from", "period_to"),
    )


class Coverage(Base):
    """This airframe is covered by this policy over this window.

    `covered_from` / `covered_to` default to the policy period and are held separately so an
    aircraft can join or leave a policy mid-term (delivery, redelivery, sale). `covered_to` NULL
    means "to the end of the policy".

    An aircraft may hold only ONE coverage at a time: the migration adds

        EXCLUDE USING gist (aircraft_id WITH =,
                            daterange(covered_from, covered_to, '[]') WITH &&)

    as `ex_coverage_no_overlap` (needs the btree_gist extension). The range is inclusive on both
    ends, so consecutive annual policies that meet on 31 Dec / 1 Jan do not collide, but a genuine
    double-insurance does. IF THE BUSINESS EVER LAYERS CONCURRENT POLICIES on one aircraft (a
    separate war-risk contract alongside the all-risks one), drop that constraint — nothing else in
    the schema depends on it. The API surfaces a violation as 409.

    The constraint is raw SQL in the migration, not metadata: Alembic autogenerate neither emits
    nor understands exclusion constraints, so keeping it out of the model is what stops a future
    autogenerate from proposing to drop it.
    """
    __tablename__ = "coverage"

    aircraft_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey(Aircraft.__table__.c.id, ondelete="RESTRICT"), nullable=False, index=True,
    )
    policy_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("policy.policy.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    covered_from: Mapped[date] = mapped_column(Date, nullable=False)
    covered_to: Mapped[Optional[date]] = mapped_column(Date, nullable=True, default=None)

    policy: Mapped["Policy"] = relationship("Policy", back_populates="coverages", lazy="selectin")

    __table_args__ = (
        CheckConstraint(
            "covered_to IS NULL OR covered_to >= covered_from", name="ck_coverage_period",
        ),
        UniqueConstraint("aircraft_id", "policy_id", "covered_from", name="uq_coverage_aircraft_policy"),
        Index("ix_coverage_period", "covered_from", "covered_to"),
    )


_current_module = sys.modules[__name__]

__all__ = [
    name
    for name, obj in globals().items()
    if inspect.isclass(obj) and obj.__module__ == __name__
]
