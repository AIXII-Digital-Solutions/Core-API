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


class ClaimsStatus(str, PyEnum):
    """Whether the claims counted by the row are closed or still moving. An `Ongoing` amount is a
    reserve and will change; a `Settled` one will not."""
    SETTLED = "Settled"
    ONGOING = "Ongoing"


_CURRENCY_ENUM = Enum(
    ClaimCurrency, name="claim_currency", schema="forecast",
    values_callable=lambda e: [m.value for m in e],
)
_POLICY_TYPE_ENUM = Enum(
    ClaimPolicyType, name="claim_policy_type", schema="forecast",
    values_callable=lambda e: [m.value for m in e],
)
_CLAIMS_STATUS_ENUM = Enum(
    ClaimsStatus, name="claims_status", schema="forecast",
    values_callable=lambda e: [m.value for m in e],
)


class AcysClaims(Base):
    """Insurance claims experience per airline and calendar year, aggregated — one row is "this
    airline had N claims worth X in this year, under this section of cover, in this state".

    NOT the same thing as `api.insurance_claims`, which records individual loss events against a
    specific aircraft and policy. This table is the summary a broker's claims-experience sheet
    states directly, loaded as given; it is a reporting input, not a derived rollup of that table.

    Grain: airline × calendar_year × currency × policy_type × claims_status. Settled and Ongoing are
    separate rows for the same year, as are two currencies — which is why `claim_amount` is never
    summed across the currency column (`claim_amounts_usd` is the one that may be).

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
    claim_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=0)
    currency: Mapped[ClaimCurrency] = mapped_column(_CURRENCY_ENUM, nullable=False)
    policy_type: Mapped[ClaimPolicyType] = mapped_column(_POLICY_TYPE_ENUM, nullable=False)
    claims_status: Mapped[ClaimsStatus] = mapped_column(_CLAIMS_STATUS_ENUM, nullable=False)

    # The currency -> USD rate applied when the row was loaded, and claim_amount converted with it.
    # `claim_amounts_usd` is the ONLY amount that may be totalled across currencies; claim_amount
    # stays in its own currency and summing that column across rows is meaningless.
    #
    # The rate is kept alongside the product, not thrown away: a converted figure that looks wrong
    # later can be traced back to the exact rate it was booked at, and a row loaded a year ago stays
    # explicable. USD rows carry rate = 1 rather than NULL, so "converted" and "not converted" are
    # the same shape.
    #
    # Nullable as a pair (enforced by ck_acys_claims_usd_pair_complete): a row written when no FX
    # source was reachable has neither, instead of a silently wrong amount.
    currency_rate: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=True, default=None)
    claim_amounts_usd: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=True, default=None)

    __table_args__ = (
        # The grain is how the table is READ — "everything this airline had in this year, by cover
        # section and state" — but it is deliberately NOT unique: duplicates are kept as sent. That
        # is why there is no upsert; loading the same sheet twice stores it twice.
        Index("ix_acys_claims_grain",
              "airline", "calendar_year", "currency", "policy_type", "claims_status"),
        # The service filters by airline before handing PowerBI its slice; year-within-airline is
        # the ordering the report reads in.
        Index("ix_acys_claims_airline_year", "airline", "calendar_year"),
        CheckConstraint("number_of_claims >= 0", name="ck_acys_claims_count_non_negative"),
        CheckConstraint("claim_amount >= 0", name="ck_acys_claims_amount_non_negative"),
        CheckConstraint("calendar_year BETWEEN 1950 AND 2200", name="ck_acys_claims_year_sane"),
        # The converted amount and the rate it came from are one fact: half of it is not auditable.
        CheckConstraint("(currency_rate IS NULL) = (claim_amounts_usd IS NULL)",
                        name="ck_acys_claims_usd_pair_complete"),
        CheckConstraint("currency_rate IS NULL OR currency_rate > 0",
                        name="ck_acys_claims_rate_positive"),
        CheckConstraint("claim_amounts_usd IS NULL OR claim_amounts_usd >= 0",
                        name="ck_acys_claims_usd_non_negative"),
    )


_current_module = sys.modules[__name__]

__all__ = [
    name
    for name, obj in globals().items()
    if inspect.isclass(obj) and obj.__module__ == __name__
]
