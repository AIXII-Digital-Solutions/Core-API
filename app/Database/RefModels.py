"""Shared reference data — schema `ref` in the aixii database.

The counterparties the insured-aircraft domain names, and nothing else. One table for every legal
entity, whatever role it plays: the same company is a lessor on one aircraft, the insured on a
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

from sqlalchemy import String, Text, BigInteger, ForeignKey, Computed, UniqueConstraint, Index
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
        "PartyContact", back_populates="party", lazy="selectin",
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
