"""Forecast-domain models (schema `forecast` in the aixii database).

Only the tables declared here are Alembic-managed. The ACYS panel's own objects — acys_actuals,
acys_forecast, acys_summary_by_day and the report matviews — live in the same schema but have NO
model on purpose (hand-written migrations create them, external-worker fills them), and
`migration/env.py`'s include_object filter keeps autogenerate from touching what it cannot see.
"""
import inspect
import sys
from decimal import Decimal
from enum import Enum as PyEnum

from sqlalchemy import (
    String, Integer, Numeric, Enum, CheckConstraint, Index,
)
from sqlalchemy.orm import Mapped, mapped_column

from .config import ForecastBase as Base


# --- Enumerations (native PG enums in schema `forecast`) ---------------------------------------
# `values_callable` stores the enum VALUE ('USD', 'Settled'), not the member NAME, so the column
# reads exactly as the source schedule writes it and PowerBI needs no translation layer. These
# classes are the single definition — the API layer imports them from `Database`.
#
# Adding a value later is a migration (ALTER TYPE ... ADD VALUE), not a code-only change. That is
# the trade for the database rejecting a typo outright instead of silently storing 'usd'.

class ClaimCurrency(str, PyEnum):
    """Currency the claim amount is denominated in. NOT converted — a total across rows is only
    meaningful within one currency."""
    USD = "USD"
    EUR = "EUR"
    GBP = "GBP"


class ClaimPolicyType(str, PyEnum):
    """Section of the cover the claims fall under.

    HD  — hull deductible
    HSL — hull & spares (legal liability)
    HW  — hull war
    WXS — war excess
    """
    HD = "HD"
    HSL = "HSL"
    HW = "HW"
    WXS = "WXS"


_CURRENCY_ENUM = Enum(
    ClaimCurrency, name="claim_currency", schema="forecast",
    values_callable=lambda e: [m.value for m in e],
)
_POLICY_TYPE_ENUM = Enum(
    ClaimPolicyType, name="claim_policy_type", schema="forecast",
    values_callable=lambda e: [m.value for m in e],
)


class AcysClaims(Base):
    """Insurance claims experience per airline and calendar year, aggregated — one row is "this
    airline had N claims worth X in this year under this section of cover, of which Y is still
    outstanding".

    NOT the same thing as `insurance.insurance_claims`, which records individual loss events against a
    specific aircraft and policy. This table is the summary a broker's claims-experience sheet
    states directly, loaded as given; it is a reporting input, not a derived rollup of that table.

    Grain: airline × calendar_year × currency × policy_type. Two currencies are separate rows —
    which is why neither amount is ever summed across the currency column (the `_usd` pair is what
    may be).

    SETTLED VS OUTSTANDING IS NO LONGER A ROW SPLIT. It used to be a `claims_status` column, so one
    year's experience arrived as two rows that had to be added up to answer "what did this year
    cost". It is now two columns of ONE row: `claims_amount_total` is everything the year cost and
    `claims_amount_outstanding` is the part of that which is still a moving reserve. The settled
    part is the difference, so nothing is lost and nothing has to be re-joined.

    The two are NOT constrained against each other on purpose. Outstanding above total is arithmetic
    nonsense, but a broker's sheet occasionally states one, and refusing the whole load over it is
    worse than storing what was sent: this table's contract is "as stated", and a reporting layer
    can flag the row.

    The grain is NOT unique: duplicates are kept exactly as sent. A sheet may legitimately state the
    same combination more than once, and the loader is not the place to decide which one is real. The
    consequence to know: loading the same sheet twice stores it twice — there is no upsert to fall
    back on, because there is no unique key for one to target.
    """
    __tablename__ = "acys_claims"

    airline: Mapped[str] = mapped_column(String, nullable=False, index=True)
    calendar_year: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    number_of_claims: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Money is NUMERIC, never float: the API accepts and returns a plain number, but the column
    # keeps exact decimal cents, so summing a column in the report cannot drift.
    claims_amount_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=0)
    claims_amount_outstanding: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False,
                                                               default=0)
    currency: Mapped[ClaimCurrency] = mapped_column(_CURRENCY_ENUM, nullable=False)
    policy_type: Mapped[ClaimPolicyType] = mapped_column(_POLICY_TYPE_ENUM, nullable=False)

    # The currency -> USD rate for THIS ROW'S calendar_year, and BOTH amounts converted with it.
    # The `_usd` columns are the ONLY amounts that may be totalled across currencies; the amounts
    # above stay in their own currency and summing those columns across rows is meaningless.
    #
    # The rate belongs to the row's own year (close-of-year), not to the day it was loaded: a 2019
    # claim is worth what it was worth in 2019, and converting it at a current rate would restate
    # history by tens of percent. A row in the current year is the exception — its 31 December has
    # not happened yet, so it carries the latest published rate and will not be revised afterwards.
    # That means two rows of the same currency in different years legitimately carry DIFFERENT
    # rates; that is the point, not an inconsistency.
    #
    # The rate is kept alongside the product, not thrown away: a converted figure that looks wrong
    # later can be traced back to the exact rate it was booked at. USD rows carry rate = 1 rather
    # than NULL, so "converted" and "not converted" are the same shape.
    #
    # Nullable as a SET (enforced by ck_acys_claims_usd_pair_complete): a row written when no FX
    # source was reachable — or one whose year predates the published series — has none of the
    # three, instead of a silently wrong amount. One rate converts both amounts, so a row can never
    # hold a total and an outstanding figure booked at different rates.
    currency_rate: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=True, default=None)
    claims_amount_total_usd: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=True,
                                                             default=None)
    claims_amount_outstanding_usd: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=True,
                                                                   default=None)

    __table_args__ = (
        # The grain is how the table is READ — "everything this airline had in this year, by cover
        # section" — but it is deliberately NOT unique: duplicates are kept as sent. That is why
        # there is no upsert; loading the same sheet twice stores it twice.
        Index("ix_acys_claims_grain", "airline", "calendar_year", "currency", "policy_type"),
        # The service filters by airline before handing PowerBI its slice; year-within-airline is
        # the ordering the report reads in.
        Index("ix_acys_claims_airline_year", "airline", "calendar_year"),
        CheckConstraint("number_of_claims >= 0", name="ck_acys_claims_count_non_negative"),
        CheckConstraint("claims_amount_total >= 0", name="ck_acys_claims_amount_non_negative"),
        CheckConstraint("claims_amount_outstanding >= 0",
                        name="ck_acys_claims_outstanding_non_negative"),
        CheckConstraint("calendar_year BETWEEN 1950 AND 2200", name="ck_acys_claims_year_sane"),
        # The converted amounts and the rate they came from are ONE fact: a fragment of it is not
        # auditable, and a row holding one converted amount but not the other could not be read.
        CheckConstraint("(currency_rate IS NULL) = (claims_amount_total_usd IS NULL) "
                        "AND (currency_rate IS NULL) = (claims_amount_outstanding_usd IS NULL)",
                        name="ck_acys_claims_usd_pair_complete"),
        CheckConstraint("currency_rate IS NULL OR currency_rate > 0",
                        name="ck_acys_claims_rate_positive"),
        CheckConstraint("claims_amount_total_usd IS NULL OR claims_amount_total_usd >= 0",
                        name="ck_acys_claims_usd_non_negative"),
        CheckConstraint("claims_amount_outstanding_usd IS NULL "
                        "OR claims_amount_outstanding_usd >= 0",
                        name="ck_acys_claims_usd_outstanding_non_negative"),
    )


_current_module = sys.modules[__name__]

__all__ = [
    name
    for name, obj in globals().items()
    if inspect.isclass(obj) and obj.__module__ == __name__
]
