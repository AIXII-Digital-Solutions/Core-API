"""Certificates — schema `certificate` in the aixii database.

An insured aircraft needs two documents, issued on request and re-issued whenever something on them
changes: the INSURANCE certificate and the REINSURANCE certificate (AVN 67B). Each table holds one of
them, every certificate from its first draft on.

A CERTIFICATE HAS TWO STATES (`status`):

  * `draft`   being prepared. Its inputs (`request`: policy, date of issue, overrides, signatory) can
              still change; its values are re-resolved from the policy, the lease and the aircraft
              whenever it is read, and every PDF drawn of it is marked DRAFT on every page.
  * `issued`  final. Its values (`data`), checks (`alerts`) and the PDF as sent (`pdf`) are frozen —
              a database trigger refuses any UPDATE or DELETE of an issued row — so it reproduces
              exactly what was sent however the policy, the lease or the aircraft change later.

THE REFERENCE NUMBER is allocated when the draft is created, so a draft circulated for review already
carries the number it will be issued under: `CY<yy>/<airline code>/<nnnnn>` — the year of the policy's
period_from, `ref.airline.certificate_code`, and nnnnn from `certificate.reference_counter`. The counter
has a SCOPE: one per certificate type by default, or one shared by both (settings
.CERTIFICATE_COUNTER_MODE). Switching never reuses a number: a scope continues from the highest value
of every scope it could collide with. A discarded draft does not give its number back.

The links to aircraft / policy / lease are for finding certificates, never for re-reading an issued
one (ON DELETE SET NULL: the history outlives the rows it was issued from).

Alembic reads THIS file (db-contract); `app/Database/CertificateModels.py` is core-api's runtime copy.
"""
import inspect
import sys
from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    String, Text, BigInteger, Integer, Date, DateTime, ForeignKey, LargeBinary, CheckConstraint,
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

DRAFT, ISSUED = "draft", "issued"


class _Certificate:
    """The columns both certificate tables share."""

    status: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'draft'"),
        comment="draft (still editable, drawn marked DRAFT) or issued (frozen).")
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

    request: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"),
        comment="The inputs: policy, date of issue, overrides, signatory — what a draft edit changes.")
    data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    alerts: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    errors: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"),
        comment="What still stops a draft being issued; always empty once issued.")
    pdf: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True, deferred=True)

    created_by_user_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_by_user_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    issued_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
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


def _table_args(name: str, what: str):
    return (
        UniqueConstraint("reference_number", name=f"uq_{name}_reference_number"),
        CheckConstraint("date_of_issue_source IN ('system', 'user')", name=f"ck_{name}_date_source"),
        CheckConstraint("status IN ('draft', 'issued')", name=f"ck_{name}_status"),
        CheckConstraint("status = 'draft' OR (pdf IS NOT NULL AND issued_at IS NOT NULL)",
                        name=f"ck_{name}_issued_complete"),
        {"comment": f"Every {what} certificate, from draft to issued. An issued row is frozen "
                    f"(trigger): its values, alerts and the PDF as sent."},
    )


class ReinsuranceCertificate(_Certificate, Base):
    """Reinsurance certificates (AVN 67B)."""
    __tablename__ = "reinsurance_certificate"
    __table_args__ = _table_args("reinsurance_certificate", "reinsurance")


class InsuranceCertificate(_Certificate, Base):
    """Insurance certificates (AVN 67B)."""
    __tablename__ = "insurance_certificate"
    __table_args__ = _table_args("insurance_certificate", "insurance")


class ReferenceCounter(Base):
    """The last sequence number handed out per scope (`reinsurance`, `insurance`, or `shared`)."""
    __tablename__ = "reference_counter"

    scope: Mapped[str] = mapped_column(String, nullable=False)
    last_value: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("scope", name="uq_certificate_reference_counter_scope"),
        CheckConstraint("last_value >= 0", name="ck_reference_counter_last_value"),
    )


class Settings(Base):
    """The issuing company as the certificates print it — ONE row (id = 1), edited from the portal's
    admin. The images (logo, stamp) are rows of `Asset`, not columns here, so this row stays small and
    its audit entries readable."""
    __tablename__ = "settings"

    company_name: Mapped[Optional[str]] = mapped_column(
        String, nullable=True, comment="The name beside the logo in the page header; empty = logo only.")
    company_legal_name: Mapped[str] = mapped_column(
        String, nullable=False, comment="As held on file by … / AUTHORISED SIGNATORY …")
    address_line: Mapped[Optional[str]] = mapped_column(
        String, nullable=True, comment="The address / phone / website line at the foot of page 1.")
    legal_footer: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, comment="The regulatory small print at the foot of page 1.")
    brand_primary: Mapped[str] = mapped_column(
        String(7), nullable=False, server_default=text("'#1F3B33'"),
        comment="Header text colour, #RRGGBB.")
    brand_accent: Mapped[str] = mapped_column(
        String(7), nullable=False, server_default=text("'#D5E28D'"),
        comment="The rule above the page number, #RRGGBB.")

    __table_args__ = (
        CheckConstraint("id = 1", name="ck_certificate_settings_single_row"),
        CheckConstraint("brand_primary ~ '^#[0-9A-Fa-f]{6}$' AND brand_accent ~ '^#[0-9A-Fa-f]{6}$'",
                        name="ck_certificate_settings_colours"),
    )


class Asset(Base):
    """An image the certificates draw: the company `logo` and `stamp`. PNG, JPEG or SVG (SVG is drawn
    as vector). Not audited: a log entry per upload would copy the bytes."""
    __tablename__ = "asset"

    key: Mapped[str] = mapped_column(String, nullable=False)
    content_type: Mapped[str] = mapped_column(String, nullable=False)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, deferred=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    updated_by_user_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    __table_args__ = (
        UniqueConstraint("key", name="uq_certificate_asset_key"),
        CheckConstraint("content_type IN ('image/png', 'image/jpeg', 'image/svg+xml')",
                        name="ck_certificate_asset_content_type"),
    )


_current_module = sys.modules[__name__]

__all__ = [
    name
    for name, obj in globals().items()
    if inspect.isclass(obj) and obj.__module__ == __name__ and not name.startswith("_")
]
