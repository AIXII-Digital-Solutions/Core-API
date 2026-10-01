"""Certificates — schema `certificate` in the aixii database.

An insured aircraft needs two documents, issued on request and re-issued whenever something on them
changes: the INSURANCE certificate and the REINSURANCE certificate (AVN 67B). Each table holds one of
them, every certificate from its first draft on.

A CERTIFICATE MOVES THROUGH FOUR STATES (`status`):

  * `draft`      being prepared. Its inputs (`request`: policy, date of issue, overrides, wording) can
                 still change; its values are re-resolved from the policy, the lease and the aircraft
                 whenever it is read, and every PDF drawn of it is marked DRAFT on every page.
  * `in_review`  submitted for review by a second person; no longer editable. Returned -> draft.
  * `approved`   the reviewer approved the values as they resolved then (`approved_hash`). Issuing
                 re-checks them; if they changed since, it goes back to review.
  * `issued`     signed electronically and final. Its values (`data`), checks (`alerts`) and the signed
                 PDF (`pdf`, byte for byte as signed) are frozen — a database trigger refuses any
                 UPDATE or DELETE of an issued row.

Every step is recorded in `events`; the last return to draft in `returned`.

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

DRAFT, IN_REVIEW, APPROVED, ISSUED = "draft", "in_review", "approved", "issued"
STATUSES = (DRAFT, IN_REVIEW, APPROVED, ISSUED)


class _Certificate:
    """The columns both certificate tables share."""

    status: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'draft'"),
        comment="draft -> in_review -> approved -> issued (frozen). Only a draft is editable.")
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
    created_by_user_email: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_by_user_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # --- the review: who submitted / approved, and the trail ({id, email, name} users)
    submitted_by_user: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by_user: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_hash: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True,
        comment="sha256 of the canonical JSON of the values approved; issuing re-checks it.")
    returned: Mapped[Optional[dict]] = mapped_column(
        JSONB, nullable=True,
        comment="The last return to draft: {by_user, at, comment, from_status}; cleared on submit.")
    events: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"),
        comment="[{action, by_user, at, comment}] oldest first: created/submitted/approved/returned/issued.")

    # --- the electronic signature
    issue_prep: Mapped[Optional[dict]] = mapped_column(
        JSONB, nullable=True, deferred=True,
        comment="The PDF prepared for signing: token hash, expiry, sha256 + length of its bytes, the "
                "values and signatory it was drawn with.")
    signature: Mapped[Optional[dict]] = mapped_column(
        JSONB, nullable=True,
        comment="The signature of the issued PDF as the portal reported it, + sha256 of the stored file.")
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
        CheckConstraint("status IN ('draft', 'in_review', 'approved', 'issued')", name=f"ck_{name}_status"),
        CheckConstraint("status <> 'issued' OR (pdf IS NOT NULL AND issued_at IS NOT NULL)",
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


# The market-standard wording a certificate quotes unless the company's settings or the certificate
# itself say otherwise. Plain ASCII, no quote characters: they are embedded in a server_default.
MARKET_WORDING = {
    "period_wording": "both days inclusive, local standard time at the address of the Insured",
    "geographical_limits": (
        "Worldwide excluding Ukraine and the region of Crimea, Iran, North Korea and Syria. However, "
        "coverage is granted (a) for the overflight of any excluded country where the flight is within "
        "an internationally recognised air corridor and is performed in accordance with I.C.A.O. "
        "recommendations; or (b) in circumstances where an insured Aircraft has landed in an excluded "
        "country as a direct consequence and exclusively as a result of force majeure. However "
        "Worldwide in respect of Products Legal Liability"),
    "hull_war_clause": "LSW 555D",
    "war_exclusion_clause": "AVN 48B",
    "war_exclusion_exception": "sub-paragraph(s) (b) of AVN48B",
    "war_liability_clause": "AVN 52E",
    "fifty_fifty_clause": "AVS103A",
}
WORDING_FIELDS = tuple(MARKET_WORDING)


def _wording(key: str, kind, comment: str):
    return mapped_column(kind, nullable=False, server_default=text(f"'{MARKET_WORDING[key]}'"),
                         comment=comment)


class Settings(Base):
    """The issuing company as the certificates print it — ONE row (id = 1), edited from the portal's
    admin. The images (logo, stamp) are rows of `Asset`, not columns here, so this row stays small and
    its audit entries readable.

    The WORDING columns are the company's defaults for what a certificate quotes; a certificate may
    override each of them in its own request (revision certificate_wording moved them off the policy)."""
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

    period_wording: Mapped[str] = _wording(
        "period_wording", Text, "How the policy period is qualified after its two dates.")
    geographical_limits: Mapped[str] = _wording(
        "geographical_limits", Text, "The Geographical Limits paragraph.")
    hull_war_clause: Mapped[str] = _wording(
        "hull_war_clause", String, "The hull war and allied perils wording the cover is in accordance with.")
    war_exclusion_clause: Mapped[str] = _wording(
        "war_exclusion_clause", String, "The war and allied perils exclusion the liability cover writes back.")
    war_exclusion_exception: Mapped[str] = _wording(
        "war_exclusion_exception", String,
        "What of the exclusion is NOT written back; empty = no exception.")
    war_liability_clause: Mapped[str] = _wording(
        "war_liability_clause", String, "The extended coverage endorsement for war liability.")
    fifty_fifty_clause: Mapped[str] = _wording(
        "fifty_fifty_clause", String, "The 50/50 provisional claims settlement clause.")

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
