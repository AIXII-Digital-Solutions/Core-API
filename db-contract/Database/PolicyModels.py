"""Insurance policies and per-aircraft coverage — schema `policy` in the aixii database.

What the insurer PROVIDES. Its twin is schema `leasing`, which holds what the lessor REQUIRES;
`combined_single_limit`, `hull_spares_war_excess_liability` and `hull_deductible_buy_down` exist on
both sides on purpose, so the two can be compared. Never collapse them.

Two levels:

    policy.policy     the contract. Every limit and deductible lives here, once, because they
                      are written for the fleet and not per tail.
    policy.policy_party  who is party to it, and as what: any number of insured, reinsured and
                      retrocedent entities per policy, in the order the schedule lists them.
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
operating airline, and the reinsurance chain is never an airline at all. Each role can name SEVERAL
entities (co-insured group companies, a panel of reinsurers), which is why they are rows of
`policy.policy_party` rather than three columns (revision policy_parties_mtow). The operating airline is
reachable through `policy.coverage -> fleet.aircraft.airline_id`.

Alembic reads THIS file (db-contract); `app/Database/PolicyModels.py` is core-api's runtime copy.
"""
import inspect
import sys
from datetime import date
from decimal import Decimal
from enum import Enum as PyEnum
from typing import Optional, List

from sqlalchemy import (
    String, Text, BigInteger, SmallInteger, Numeric, Date, ForeignKey, Enum,
    UniqueConstraint, CheckConstraint, Index, text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .config import PolicyBase as Base
# A ForeignKey STRING is resolved inside the OWNING MetaData, which cannot see another
# Base's tables, so every CROSS-SCHEMA link below is made with the Column object.
from .RefModels import Party, CURRENCY_VALUES
from .FleetModels import Aircraft


# The market-standard wording the certificate columns default to. Plain ASCII, no quote characters:
# they are embedded in a server_default.
DEFAULT_PERIOD_WORDING = "both days inclusive, local standard time at the address of the Insured"
DEFAULT_GEOGRAPHICAL_LIMITS = (
    "Worldwide excluding Ukraine and the region of Crimea, Iran, North Korea and Syria. However, "
    "coverage is granted (a) for the overflight of any excluded country where the flight is within "
    "an internationally recognised air corridor and is performed in accordance with I.C.A.O. "
    "recommendations; or (b) in circumstances where an insured Aircraft has landed in an excluded "
    "country as a direct consequence and exclusively as a result of force majeure. However "
    "Worldwide in respect of Products Legal Liability"
)


class PartyRole(PyEnum):
    """The part an entity plays in one policy. Stored as the VALUE."""
    INSURED = "insured"
    REINSURED = "reinsured"
    RETROCEDENT = "retrocedent"


_ROLE_ENUM = Enum(PartyRole, name="party_role", schema="policy",
                  values_callable=lambda e: [m.value for m in e])


class Policy(Base):
    """One insurance contract over one period.

    NO NATURAL KEY. It used to be (insured, period), which stopped meaning anything once a policy
    could name several insured entities (revision policy_parties_mtow). Nothing else identifies a
    policy either — the schedules carry no number — so two policies with the same parties and
    period are possible, and the write path is where duplicates have to be avoided.

    THE WAR COLUMNS. `hull_war_confiscation_limit` is the general confiscation limit;
    `hull_war_confiscation_limit_selected_country` is the (usually much lower) limit that applies
    to flights into the one territory named in `selected_country`, e.g. Russia. Both are limits on
    the same peril, which is why they sit side by side rather than in separate rows.

    `currency` is the contract's: every amount on the policy is in it. It was held per aircraft in
    `fleet.service_info.policy_currency` for a while and came back here in revision
    policy_currency_on_policy, so the aircraft on one policy can no longer disagree about it.

    `cut_through_clause` is the clause text itself — it is quoted in full in correspondence, and
    what matters is the wording, not a flag saying one exists.
    """
    __tablename__ = "policy"

    period_from: Mapped[date] = mapped_column(Date, nullable=False)
    period_to: Mapped[Optional[date]] = mapped_column(Date, nullable=True, default=None)
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, server_default=text("'USD'"),
        comment="The currency every amount on this policy is in.",
    )

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

    # --- reinsurance. PERCENTS, not fractions: `reinsured_amount` of `reinsured_amount_of` —
    # 97.5 of 100 reads "97.5 % of the 100 % share" — so a cession off a partial share is stated
    # as the schedule states it, not pre-multiplied.
    reinsured_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 3), nullable=True, default=None)
    reinsured_amount_of: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(6, 3), nullable=True, default=None,
        comment="The share, in percent, that reinsured_amount is a percentage of (97.5 of 100).",
    )

    cut_through_clause: Mapped[Optional[str]] = mapped_column(Text, nullable=True, default=None)

    # --- wording the certificates quote from the policy. Each defaults to the market-standard form
    # and is edited per policy where the schedule says otherwise (revision certificates_schema).
    period_wording: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text(f"'{DEFAULT_PERIOD_WORDING}'"),
        comment="How the period is qualified after its two dates.")
    geographical_limits: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text(f"'{DEFAULT_GEOGRAPHICAL_LIMITS}'"),
        comment="The Geographical Limits paragraph, as the policy words it.")
    hull_war_clause: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'LSW 555D'"),
        comment="The hull war and allied perils wording the cover is in accordance with.")
    war_exclusion_clause: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'AVN 48B'"),
        comment="The war and allied perils exclusion clause the liability cover writes back.")
    war_exclusion_exception: Mapped[Optional[str]] = mapped_column(
        String, nullable=True, server_default=text("'sub-paragraph(s) (b) of AVN48B'"),
        comment="What of the exclusion is NOT written back, e.g. sub-paragraph(s) (b) of AVN48B.")
    war_liability_clause: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'AVN 52E'"),
        comment="The extended coverage endorsement for war liability.")
    fifty_fifty_clause: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'AVS103A'"),
        comment="The 50/50 provisional claims settlement clause.")

    parties: Mapped[List["PolicyParty"]] = relationship(
        "PolicyParty", back_populates="policy", lazy="raise_on_sql",
        order_by="(PolicyParty.role, PolicyParty.position, PolicyParty.id)",
        cascade="all, delete-orphan",
    )
    coverages: Mapped[List["Coverage"]] = relationship(
        "Coverage", back_populates="policy", lazy="raise_on_sql",
    )

    __table_args__ = (
        CheckConstraint("period_to IS NULL OR period_to >= period_from", name="ck_policy_period"),
        CheckConstraint(f"currency IN ({CURRENCY_VALUES}) AND currency = upper(currency)",
                        name="ck_policy_currency"),
        CheckConstraint(
            "reinsured_amount IS NULL OR reinsured_amount BETWEEN 0 AND 100",
            name="ck_policy_reinsured_amount",
        ),
        CheckConstraint(
            "reinsured_amount_of IS NULL OR reinsured_amount_of BETWEEN 0 AND 100",
            name="ck_policy_reinsured_amount_of",
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


class PolicyParty(Base):
    """One entity in one role on one policy. `position` is the order the schedule lists them in,
    1-based per role; the first insured is the one a one-line summary shows."""
    __tablename__ = "policy_party"

    policy_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("policy.policy.id", ondelete="CASCADE"), nullable=False,
        index=False,   # uq_policy_party leads with it
    )
    party_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey(Party.__table__.c.id, ondelete="RESTRICT"), nullable=False,
        index=True,
    )
    role: Mapped[PartyRole] = mapped_column(_ROLE_ENUM, nullable=False)
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("1"))

    policy: Mapped["Policy"] = relationship("Policy", back_populates="parties", lazy="raise_on_sql")
    party: Mapped["Party"] = relationship(Party, lazy="raise_on_sql")

    __table_args__ = (
        UniqueConstraint("policy_id", "role", "party_id", name="uq_policy_party"),
        CheckConstraint("position >= 1", name="ck_policy_party_position"),
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
        BigInteger, ForeignKey(Aircraft.__table__.c.id, ondelete="RESTRICT"), nullable=False,
        index=False,   # uq_coverage_aircraft_policy leads with it
    )
    policy_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("policy.policy.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    covered_from: Mapped[date] = mapped_column(Date, nullable=False)
    covered_to: Mapped[Optional[date]] = mapped_column(Date, nullable=True, default=None)

    policy: Mapped["Policy"] = relationship("Policy", back_populates="coverages", lazy="raise_on_sql")

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
