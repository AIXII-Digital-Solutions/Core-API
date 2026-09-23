"""Shared reference data — schema `ref` in the aixii database.

The two things every other schema in the insured-aircraft domain points at: the AIRLINE that
operates an aircraft, and the COUNTERPARTIES a contract names.

`ref.airline` moved here from `api.airlines` (revision `airlines_to_ref`) once the domain was
rebuilt around it. It is deliberately NOT merged into `ref.party`: an airline carries an ICAO and
an IATA code and is matched on them, a counterparty is matched on its name, and the two are
different in every source the platform reads. The cirium asg matviews still resolve their operator
strings against this table — moving it was a catalogue update, so those matviews never noticed.

One table for every counterparty, whatever role it plays: the same company is a lessor on one aircraft, the insured on a
policy and a retrocedent on another, so separate per-role tables would hold three copies of it and
let them drift. The ROLE is decided by the referencing column
(`leasing.agreement.lessor_id`, `policy.policy.insured_id` / `reinsured_id` / `retrocedent_id`) —
never by a flag on the party itself.

`name_normalized` is a STORED generated column carrying the unique constraint, so an importer
cannot turn 'AerCap' and 'AERCAP ' into two entities.

Alembic reads THIS file (db-contract); `app/Database/RefModels.py` is core-api's runtime copy and
has to be updated by hand.
"""
import inspect
import sys
from typing import Optional, List

from sqlalchemy import (
    String, Text, BigInteger, Boolean, ForeignKey, Computed, UniqueConstraint, Index, text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .config import RefBase as Base

# The currencies a contract may be written in (groups 5.3 / 5.4 of the spec). Kept as a CHECK on a
# CHAR(3) rather than a native enum: adding a currency is then one migration that touches no type
# used by two schemas, and the value is readable everywhere without a cast.
CURRENCIES = ("USD", "EUR", "GBP")
CURRENCY_VALUES = ", ".join(f"'{c}'" for c in CURRENCIES)


def currency_check(column: str = "currency") -> str:
    """The CHECK body for a currency column, so leasing and policy cannot disagree about it."""
    return f"{column} IN ({CURRENCY_VALUES})"


class Airline(Base):
    """An operating airline. The platform's own hand-kept reference, ~21 rows, not a directory:
    a carrier is here because this business insures or tracks its fleet.

    `is_asg` decides which cirium matview picks the carrier's aircraft up, and therefore which
    tails FlightRadar is polled for — TRUE feeds `cirium.asg_*`, FALSE feeds
    `cirium.non_asg_insured_*`. Changing it means the matviews must be refreshed before anything
    downstream sees the difference.

    Matching against this table is by SUBSTRING, longest name first, because Cirium writes
    "Air Arabia Abu Dhabi" where this table holds "Air Arabia". That is why the names here stay
    short and generic; do not lengthen one to make a single row match.
    """
    __tablename__ = "airline"

    airline_name: Mapped[str] = mapped_column(String, index=True)
    icao: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True, default=None)
    iata: Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True, default=None)
    is_asg: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"),
        comment="TRUE = an ASG airline (cirium.asg_*); FALSE = insured but not ASG "
                "(cirium.non_asg_insured_*).",
    )
    logo_url: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, default=None,
        comment="URL into the platform image store. Never the bytes - a grid reads every airline "
                "on the page and a blob per row would drag megabytes through the connection.",
    )

    __table_args__ = (
        {"comment": "The airlines this business insures or tracks - a hand-kept reference, not a "
                    "directory. Moved from api.airlines by revision airlines_to_ref. Operator "
                    "strings from Cirium are matched against it by SUBSTRING, longest name first, "
                    "so the names here stay short."},
    )


class Party(Base):
    """A counterparty: lessor, insured, reinsured, retrocedent — and whatever role comes next.

    `details` is the free-text note the portal shows under the name (what this entity is, which
    group it belongs to, anything the schedule carried that has no column of its own). The
    structured contact blocks live one level down, in `ref.party_contact`.
    """
    __tablename__ = "party"

    name: Mapped[str] = mapped_column(String, nullable=False)
    name_normalized: Mapped[str] = mapped_column(
        String, Computed("upper(btrim(name))", persisted=True), nullable=False,
    )
    details: Mapped[Optional[str]] = mapped_column(Text, nullable=True, default=None)

    contacts: Mapped[List["PartyContact"]] = relationship(
        "PartyContact", back_populates="party", lazy="raise_on_sql",
        order_by="PartyContact.id", cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint("name_normalized", name="uq_party_name_normalized"),
    )


class PartyContact(Base):
    """One contact block, as the source documents write them:

        COMPANY:  BOC Aviation (Ireland) Limited
        CONTACTS: Insurance
        EMAIL:    insurance@bocaviation.com

    A party routinely carries several of these (one per group entity, one per department), which is
    why this is rows and not a text blob on `ref.party` — the portal renders them as a list, and an
    address can be searched for.

    `company` is the entity named on the block, which is NOT always the party's own name: a group
    lists its subsidiaries here. Every field is optional because these blocks arrive half-filled.
    """
    __tablename__ = "party_contact"

    party_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("ref.party.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    company: Mapped[Optional[str]] = mapped_column(String, nullable=True, default=None)
    contact: Mapped[Optional[str]] = mapped_column(String, nullable=True, default=None)
    email: Mapped[Optional[str]] = mapped_column(String, nullable=True, default=None)
    phone: Mapped[Optional[str]] = mapped_column(String, nullable=True, default=None)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True, default=None)

    party: Mapped["Party"] = relationship("Party", back_populates="contacts")

    __table_args__ = (
        Index("ix_party_contact_email", "email"),
    )


_current_module = sys.modules[__name__]

__all__ = [
    name
    for name, obj in globals().items()
    if inspect.isclass(obj) and obj.__module__ == __name__
]
