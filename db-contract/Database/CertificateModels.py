"""Issued certificates — schema `certificate` in the aixii database.

An insured aircraft needs two documents, issued on request and re-issued whenever something on them
changes: the INSURANCE certificate and the REINSURANCE certificate (AVN 67B). Each table here is the
history of one of them — every certificate ever issued, never updated, never deleted by the API.

WHAT A ROW HOLDS. The certificate is a legal document, so a row must reproduce exactly what was
sent, however the policy, the lease or the aircraft have changed since:

  * `data`       every value printed on it, resolved and formatted at the moment of issue;
  * `alerts`     the warnings the checks raised (a lease limit above the policy's, …);
  * `pdf`        the rendered document itself, deferred so a listing never drags it along;
  * the links    aircraft / policy / lease — for finding certificates, never for re-reading
                 them (ON DELETE SET NULL: the history outlives the rows it was issued from).

THE REFERENCE NUMBER is `CY<yy>/<airline code>/<nnnnn>`: the contract year is the year of the policy's
period_from, the airline code is `ref.airline.certificate_code`, and nnnnn comes from
`certificate.reference_counter`. The counter has a SCOPE: one per certificate type by default, or
one shared scope for both types (settings.CERTIFICATE_COUNTER_MODE). Switching between them never
reuses a number: a scope continues from the highest value of every scope it could collide with.

Alembic reads THIS file (db-contract); `app/Database/CertificateModels.py` is core-api's runtime copy.
"""
import inspect
import sys
from datetime import date
from typing import Optional

from sqlalchemy import (
    String, BigInteger, Integer, Date, ForeignKey, LargeBinary, CheckConstraint,
    UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, declared_attr

from .config import CertificateBase as Base
# A ForeignKey STRING is resolved inside the OWNING MetaData, which cannot see another Base's
# tables, so every CROSS-SCHEMA link below is made with the Column object.
from .FleetModels import Aircraft
from .PolicyModels import Policy
from .LeasingModels import AircraftLease


class _IssuedCertificate:
    """The columns both certificate tables share."""

    reference_number: Mapped[str] = mapped_column(String, nullable=False)
    contract_year: Mapped[int] = mapped_column(Integer, nullable=False)
    airline_code: Mapped[str] = mapped_column(String, nullable=False)
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    counter_scope: Mapped[str] = mapped_column(String, nullable=False)

    date_of_issue: Mapped[date] = mapped_column(Date, nullable=False)
    date_of_issue_source: Mapped[str] = mapped_column(
        String, nullable=False, comment="system (the day it was issued) or user (sent with the request)")
    variant: Mapped[str] = mapped_column(
        String, nullable=False, comment="Which wording the document uses, e.g. retrocession / standard.")
    template_version: Mapped[str] = mapped_column(String, nullable=False)

    registration: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    msn: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    alerts: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    pdf: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, deferred=True)

    issued_by: Mapped[Optional[str]] = mapped_column(String, nullable=True,
                                                     comment="The API credential, as in audit.change_log.")
    issued_by_user_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    issued_by_user_email: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    issued_by_user_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    @declared_attr
    def aircraft_id(cls) -> Mapped[Optional[int]]:
        return mapped_column(BigInteger, ForeignKey(Aircraft.__table__.c.id, ondelete="SET NULL"),
                             nullable=True, index=True)

    @declared_attr
    def policy_id(cls) -> Mapped[Optional[int]]:
        return mapped_column(BigInteger, ForeignKey(Policy.__table__.c.id, ondelete="SET NULL"),
                             nullable=True, index=True)

    @declared_attr
    def aircraft_lease_id(cls) -> Mapped[Optional[int]]:
        return mapped_column(BigInteger,
                             ForeignKey(AircraftLease.__table__.c.id, ondelete="SET NULL"),
                             nullable=True, index=True)


class ReinsuranceCertificate(_IssuedCertificate, Base):
    """Every reinsurance certificate issued (AVN 67B)."""
    __tablename__ = "reinsurance_certificate"
    __table_args__ = (
        UniqueConstraint("reference_number", name="uq_reinsurance_certificate_reference_number"),
        CheckConstraint("date_of_issue_source IN ('system', 'user')", name="ck_reinsurance_certificate_date_source"),
        {"comment": "Every reinsurance certificate issued: its reference, the values printed on it, "
                    "the checks' alerts and the PDF as sent. Append-only."},
    )


class InsuranceCertificate(_IssuedCertificate, Base):
    """Every insurance certificate issued."""
    __tablename__ = "insurance_certificate"
    __table_args__ = (
        UniqueConstraint("reference_number", name="uq_insurance_certificate_reference_number"),
        CheckConstraint("date_of_issue_source IN ('system', 'user')", name="ck_insurance_certificate_date_source"),
        {"comment": "Every insurance certificate issued: its reference, the values printed on it, "
                    "the checks' alerts and the PDF as sent. Append-only."},
    )


class ReferenceCounter(Base):
    """The last sequence number handed out per scope (`reinsurance`, `insurance`, or `shared`)."""
    __tablename__ = "reference_counter"

    scope: Mapped[str] = mapped_column(String, nullable=False)
    last_value: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("scope", name="uq_certificate_reference_counter_scope"),
        CheckConstraint("last_value >= 0", name="ck_reference_counter_last_value"),
    )


_current_module = sys.modules[__name__]

__all__ = [
    name
    for name, obj in globals().items()
    if inspect.isclass(obj) and obj.__module__ == __name__ and not name.startswith("_")
]
