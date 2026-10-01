"""Certificates for insured aircraft (AVN 67B): `/certificates/reinsurance` and `/certificates/insurance`.

Both documents share one flow, one set of endpoints per kind:

    POST   /certificates/{kind}/preview              what it would say — nothing saved
    POST   /certificates/{kind}                      create a DRAFT; its reference number is allocated now
    GET    /certificates/{kind}                      the certificates of that kind, newest first
    GET    /certificates/{kind}/{id}                 one, with its trail (`events`)
    PATCH  /certificates/{kind}/{id}                 change a draft's inputs            (draft only)
    DELETE /certificates/{kind}/{id}                 discard a draft                    (draft only)
    POST   /certificates/{kind}/{id}/submit          draft -> in_review
    POST   /certificates/{kind}/{id}/approve         in_review -> approved              (not the submitter)
    POST   /certificates/{kind}/{id}/return          in_review | approved -> draft      (comment required)
    POST   /certificates/{kind}/{id}/issue/prepare   the final PDF with an empty signature field + a token
    POST   /certificates/{kind}/{id}/issue           upload the signed PDF: approved -> issued
    GET    /certificates/{kind}/{id}/pdf             not yet issued: drawn now, marked DRAFT; issued: as signed

    GET/PATCH      /certificates/settings                the issuing company as printed, and its default
                                                          certificate wording (admin)
    PUT/GET/DELETE /certificates/settings/{logo|stamp}   its images (admin)

THE REVIEW. Two people: one submits, another approves (`same_person` otherwise), and the one who signs
is not the approver. Approval records the hash of the values as they resolved (`signing.approved_hash`);
preparing the signature re-resolves them and sends the certificate back to review if they changed.

THE SIGNATURE. `issue/prepare` draws the final PDF — no DRAFT mark, the signatory printed — with an
empty AcroForm signature field `Signatory`, and hands out a single-use token (15 minutes). The portal
signs that field electronically and uploads the result to `issue` with the token; the file must be the
prepared PDF plus a valid signature and nothing else (`signing.verify_signed`). The signed bytes are
the issued PDF: never re-drawn, since any re-render would break the signature.

The people come from the X-Portal-User-* headers (`current_actor()`); a step without a portal user is
refused (`portal_user_required`). Every refusal carries `details.code`.
"""
import base64
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Literal, Optional

from fastapi import Depends, File, Form, Query, Request, Response, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.orm import undefer

from Certificates import assemble as assembly, issuer as issuer_mod, numbering, signing
from Certificates.render import SIGNATURE_FIELD, render
from Config import setup_logger
from Database import ApiToken
from Database.CertificateModels import (APPROVED, DRAFT, IN_REVIEW, ISSUED, MARKET_WORDING,
                                        WORDING_FIELDS, Asset, InsuranceCertificate,
                                        ReinsuranceCertificate, Settings)
from api_auth import authorize, current_actor, SCOPE_INSURANCE_READ, SCOPE_INSURANCE_WRITE
from settings import Router
from Utils import error_response, success_response, warning_response
from Utils.DomainCommon import DB, iso, page_with_total, set_actor
from Utils.ResponsesFunc import build_responses

logger = setup_logger("certificates_api")

router = Router(prefix="/certificates", tags=["Certificates"])

_READ = [Depends(authorize(SCOPE_INSURANCE_READ))]
_OK = {status.HTTP_200_OK, status.HTTP_400_BAD_REQUEST, status.HTTP_404_NOT_FOUND,
       status.HTTP_409_CONFLICT, status.HTTP_500_INTERNAL_SERVER_ERROR}
MODELS = {assembly.REINSURANCE: ReinsuranceCertificate, assembly.INSURANCE: InsuranceCertificate}
_TITLE = {assembly.REINSURANCE: "Reinsurance", assembly.INSURANCE: "Insurance"}
MAX_SIGNED_BYTES = 20 * 1024 * 1024


# ==============================================================================================
# bodies
# ==============================================================================================

class Addressee(BaseModel):
    company: str = Field(min_length=1, max_length=256)
    contacts: Optional[str] = Field(default=None, max_length=256)
    email: Optional[str] = Field(default=None, max_length=256)


class SignatoryIn(BaseModel):
    """Who signs the certificate — printed under the signature."""
    full_name: str = Field(min_length=1, max_length=256)
    title: Optional[str] = Field(default=None, max_length=128, description="e.g. Head of Insurance.")
    phone: Optional[str] = Field(default=None, max_length=64)
    email: Optional[str] = Field(default=None, max_length=256)


class _Wording(BaseModel):
    """The wording a certificate quotes. Each defaults to the company's setting
    (`/certificates/settings`), which defaults to the market-standard text."""
    period_wording: Optional[str] = Field(
        default=None, min_length=1, max_length=2000,
        description="How the policy period is qualified after its two dates.")
    geographical_limits: Optional[str] = Field(
        default=None, min_length=1, max_length=2000, description="The Geographical Limits paragraph.")
    hull_war_clause: Optional[str] = Field(default=None, min_length=1, max_length=256,
                                           description="e.g. LSW 555D.")
    war_exclusion_clause: Optional[str] = Field(default=None, min_length=1, max_length=256,
                                                description="e.g. AVN 48B.")
    war_exclusion_exception: Optional[str] = Field(
        default=None, max_length=256,
        description="What of the exclusion is not written back, e.g. 'sub-paragraph(s) (b) of "
                    "AVN48B'. \"\" = no exception.")
    war_liability_clause: Optional[str] = Field(default=None, min_length=1, max_length=256,
                                                description="e.g. AVN 52E.")
    fifty_fifty_clause: Optional[str] = Field(default=None, min_length=1, max_length=256,
                                              description="e.g. AVS103A.")


class _Inputs(_Wording):
    """Everything a certificate's inputs can say besides the aircraft. On PATCH, a field sent
    replaces the draft's; sent as null, it drops the override and the stored value (or the company's
    wording) applies again. `war_exclusion_exception: ""` is an override ("no exception"), not null.
    The signatory is not an input: it is given when the PDF is prepared for signing."""
    policy_id: Optional[int] = Field(
        default=None, description="Default: the policy covering the aircraft on the date of issue, "
                                  "else the next one to start.")
    date_of_issue: Optional[date] = Field(
        default=None, description="Default: the day the PDF is prepared for signing (system-generated).")
    signed_by: Optional[Literal["broker", "insurer"]] = Field(
        default=None, description="Insurance certificate only: who signs it — `broker` (us, as the "
                                  "Insured's insurance broker; default) or `insurer`.")
    agreed_value: Optional[Decimal] = Field(default=None, ge=0,
                                            description="Override of the lease's agreed value (A10).")
    equipment: Optional[str] = Field(default=None, max_length=256,
                                     description="Override of '<manufacturer> <series>' (A7).")
    effective_date: Optional[date] = Field(default=None,
                                           description="Override of the lease terms' effective date (A28).")
    contract_parties: Optional[list[str]] = Field(default=None, min_length=1, max_length=20,
                                                  description="Override of the contract parties (A24), in order.")
    contracts: Optional[list[str]] = Field(default=None, min_length=1, max_length=20,
                                           description="Override of the contracts list (A25-A27).")
    addressees: Optional[list[Addressee]] = Field(default=None, max_length=20,
                                                  description="Override of the notice addresses (A29).")


class CertificateIn(_Inputs):
    aircraft_id: int


class CertificatePatch(_Inputs):
    """Only the fields sent change. The aircraft cannot: a certificate for another aircraft is
    another certificate."""


class CommentIn(BaseModel):
    comment: Optional[str] = Field(default=None, max_length=2000)


class ReturnIn(BaseModel):
    comment: str = Field(min_length=1, max_length=2000, description="Why it goes back to draft.")


class PrepareIn(BaseModel):
    signatory: SignatoryIn


class SignatureIn(BaseModel):
    """The signature as the portal's signing tool reports it. `sha256` is not sent: the API computes
    it over the file it stores."""
    signer_name: Optional[str] = Field(default=None, max_length=256)
    signer_email: Optional[str] = Field(default=None, max_length=256)
    subject: Optional[str] = Field(default=None, max_length=1000, description="The certificate subject DN.")
    issuer: Optional[str] = Field(default=None, max_length=1000, description="The certificate issuer DN.")
    serial: Optional[str] = Field(default=None, max_length=128)
    valid_from: Optional[datetime] = None
    valid_to: Optional[datetime] = None
    signed_at: Optional[datetime] = None
    timestamped: bool = False
    level: Optional[str] = Field(default=None, max_length=32, description="e.g. PAdES-B-T.")


class SettingsPatch(_Wording):
    """The issuing company as the certificates print it, and its default certificate wording. Only
    the fields sent change; send `company_name`, `address_line` or `legal_footer` as null (or "") to
    leave them off. A wording field sent as null returns to the market-standard text;
    `war_exclusion_exception: ""` means no exception."""
    company_name: Optional[str] = Field(default=None, max_length=128,
                                        description="Beside the logo in the page header.")
    company_legal_name: Optional[str] = Field(
        default=None, min_length=1, max_length=256,
        description="'as held on file by …' and 'AUTHORISED SIGNATORY …'.")
    address_line: Optional[str] = Field(default=None, max_length=256,
                                        description="The line at the foot of page 1.")
    legal_footer: Optional[str] = Field(default=None, max_length=2000,
                                        description="The regulatory small print on page 1.")
    brand_primary: Optional[str] = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    brand_accent: Optional[str] = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")


# ==============================================================================================
# resolving a certificate from its inputs
# ==============================================================================================

def _stored_inputs(body: BaseModel, *, exclude_unset: bool) -> dict:
    return body.model_dump(mode="json", exclude_unset=exclude_unset, exclude={"aircraft_id"})


def _overrides(req: dict) -> assembly.Overrides:
    return assembly.Overrides(
        agreed_value=Decimal(str(req["agreed_value"])) if req.get("agreed_value") is not None else None,
        equipment=req.get("equipment"),
        effective_date=date.fromisoformat(req["effective_date"]) if req.get("effective_date") else None,
        contract_parties=req.get("contract_parties"), contracts=req.get("contracts"),
        addressees=req.get("addressees"))


class _Resolved:
    def __init__(self, draft, images, date_of_issue, errors):
        self.draft, self.images, self.date_of_issue, self.errors = draft, images, date_of_issue, errors


async def _resolve(session, kind: str, aircraft_id: int, req: dict, *,
                   signatory: Optional[dict] = None) -> _Resolved:
    """The certificate as its inputs make it today: values, alerts, errors, the issuer, and the
    images to draw it with. The signatory is printed only once it is known (`issue/prepare`)."""
    date_of_issue = date.fromisoformat(req["date_of_issue"]) if req.get("date_of_issue") else date.today()
    issuer, company_wording, images = await issuer_mod.load(session)
    draft = await assembly.assemble(
        session, kind, aircraft_id=aircraft_id, date_of_issue=date_of_issue,
        policy_id=req.get("policy_id"), overrides=_overrides(req),
        signed_by=req.get("signed_by") or "broker",
        wording=issuer_mod.resolve_wording(req, company_wording))
    draft.data["issuer"] = issuer
    draft.data["signatory"] = signatory
    return _Resolved(draft, images, date_of_issue, list(draft.errors))


def _errors_out(errors) -> list:
    return [{"field": f"certificate.{e['field']}", "msg": e["msg"]} for e in errors]


_NUMBERING_FIELDS = {"airline", "certificate_code"}


# ==============================================================================================
# people, refusals, the record
# ==============================================================================================

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _portal_user() -> Optional[dict]:
    _kind, user = current_actor()
    return {"id": user.id, "email": user.email, "name": user.name} if user is not None else None


def _same(a: Optional[dict], b: Optional[dict]) -> bool:
    return bool(a and b and a.get("id") and a.get("id") == b.get("id"))


def _event(row, action: str, user: Optional[dict], comment: Optional[str] = None) -> None:
    row.events = [*(row.events or []),
                  {"action": action, "by_user": user, "at": _now().isoformat(), "comment": comment}]


def _fail(request, response, http: int, code: str, msg: str, data=None):
    return warning_response(request=request, response=response, msg=msg, status_code=http,
                            code=code, data=data)


def _not_found(request, response, kind, certificate_id):
    return _fail(request, response, status.HTTP_404_NOT_FOUND, "not_found",
                 f"{_TITLE[kind]} certificate {certificate_id} not found")


def _wrong_status(request, response, row, allowed: str):
    return _fail(request, response, status.HTTP_409_CONFLICT, "wrong_status",
                 f"Certificate {row.reference_number} is {row.status}; this needs it {allowed}.")


def _incomplete(request, response, errors):
    return _fail(request, response, status.HTTP_422_UNPROCESSABLE_ENTITY, "incomplete",
                 "The certificate is missing required values.", data=_errors_out(errors))


def _no_user(request, response):
    return _fail(request, response, status.HTTP_422_UNPROCESSABLE_ENTITY, "portal_user_required",
                 "This step is taken by a person: send the X-Portal-User-* headers.")


def _record(kind: str, row, *, live: Optional[_Resolved] = None, with_data: bool = True) -> dict:
    errors = live.errors if live else row.errors
    out = {
        "id": row.id,
        "kind": kind,
        "status": row.status,
        "reference_number": row.reference_number,
        "date_of_issue": iso(live.date_of_issue if live else row.date_of_issue),
        "date_of_issue_source": row.date_of_issue_source,
        "variant": live.draft.variant if live else row.variant,
        "registration": row.registration,
        "msn": row.msn,
        "aircraft_id": row.aircraft_id,
        "policy_id": live.draft.policy_id if live else row.policy_id,
        "aircraft_lease_id": live.draft.aircraft_lease_id if live else row.aircraft_lease_id,
        "alerts": live.draft.alerts if live else row.alerts,
        "errors": _errors_out(errors),
        "can_submit": row.status == DRAFT and not errors,
        "can_issue": row.status == APPROVED and not errors,
        "created_by_user": ({"id": row.created_by_user_id, "email": row.created_by_user_email,
                             "name": row.created_by_user_name}
                            if row.created_by_user_id or row.created_by_user_name else None),
        "created_at": iso(row.created_at),
        "updated_at": iso(row.updated_at),
        "submitted_by_user": row.submitted_by_user,
        "submitted_at": iso(row.submitted_at),
        "approved_by_user": row.approved_by_user,
        "approved_at": iso(row.approved_at),
        "returned": row.returned,
        "issued_at": iso(row.issued_at),
        "issued_by": row.issued_by,
        "issued_by_user": ({"id": row.issued_by_user_id, "email": row.issued_by_user_email,
                            "name": row.issued_by_user_name}
                           if row.issued_by_user_id or row.issued_by_user_name else None),
        "signature": row.signature,
        "request": row.request,
        "pdf_url": f"/certificates/{kind}/{row.id}/pdf",
    }
    if with_data:      # a single certificate: its values and its trail
        out["data"] = live.draft.data if live else row.data
        out["events"] = row.events or []
    return out


def _apply(row, kind: str, resolved: _Resolved, req: dict) -> None:
    """Write a resolution onto a row not yet issued. The sequence number stays; the reference is
    rebuilt in case the policy (contract year) or the airline code changed."""
    d = resolved.draft
    row.request = req
    row.data, row.alerts, row.errors = d.data, d.alerts, resolved.errors
    row.date_of_issue = resolved.date_of_issue
    row.date_of_issue_source = "user" if req.get("date_of_issue") else "system"
    row.variant, row.template_version = d.variant, assembly.TEMPLATE_VERSION[kind]
    row.registration, row.msn = d.registration, d.msn
    row.policy_id, row.aircraft_lease_id = d.policy_id, d.aircraft_lease_id
    if d.airline_code and d.contract_year:
        row.airline_code, row.contract_year = d.airline_code, d.contract_year
        row.reference_number = numbering.reference_number(d.contract_year, d.airline_code,
                                                          row.sequence_no)


def _clear_approval(row) -> None:
    row.approved_by_user = row.approved_at = row.approved_hash = None
    row.issue_prep = None


async def _load(session, kind: str, certificate_id: int, *, lock: bool = False, prep: bool = False):
    stmt = select(MODELS[kind]).where(MODELS[kind].id == certificate_id)
    if prep:
        stmt = stmt.options(undefer(MODELS[kind].issue_prep))
    if lock:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def _live(session, kind: str, row) -> Optional[_Resolved]:
    """A certificate not yet issued is shown as it resolves now; an issued one as frozen."""
    if row.status == ISSUED or row.aircraft_id is None:
        return None
    return await _resolve(session, kind, row.aircraft_id, row.request)


# ==============================================================================================
# the endpoints, once per kind
# ==============================================================================================

def _register(kind: str) -> None:
    Model = MODELS[kind]
    title = _TITLE[kind]
    base = f"/{kind}"

    def not_found(request, response, ex):
        return _fail(request, response, status.HTTP_404_NOT_FOUND, "not_found", str(ex))

    async def preview(request: Request, response: Response, body: CertificateIn):
        try:
            async with request.app.state.db_client.read_session(DB) as session:
                r = await _resolve(session, kind, body.aircraft_id,
                                   _stored_inputs(body, exclude_unset=True))
            ref = (numbering.reference_number(r.draft.contract_year, r.draft.airline_code, 0)
                   .replace("00000", "#####") if r.draft.airline_code and r.draft.contract_year else None)
            return success_response(request=request, response=response, data={
                "reference_number": ref, "date_of_issue": r.date_of_issue.isoformat(),
                "variant": r.draft.variant, "data": r.draft.data, "alerts": r.draft.alerts,
                "errors": _errors_out(r.errors), "can_submit": not r.errors})
        except assembly.NotFound as _ex:
            return not_found(request, response, _ex)
        except Exception as _ex:
            return error_response(request=request, exc=_ex, response=response)

    async def create(request: Request, response: Response, body: CertificateIn,
                     token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
        try:
            req = _stored_inputs(body, exclude_unset=True)
            user = _portal_user()
            async with request.app.state.db_client.session(DB) as session:
                await set_actor(session, token)
                r = await _resolve(session, kind, body.aircraft_id, req)
                blocking = [e for e in r.errors if e["field"] in _NUMBERING_FIELDS]
                if blocking:   # without an airline code there is no reference number to give it
                    return _incomplete(request, response, blocking)
                scope, sequence = await numbering.next_sequence(session, kind)
                row = Model(status=DRAFT, sequence_no=sequence, counter_scope=scope,
                            aircraft_id=body.aircraft_id,
                            created_by_user_id=user["id"] if user else None,
                            created_by_user_email=user["email"] if user else None,
                            created_by_user_name=user["name"] if user else None)
                _apply(row, kind, r, req)
                _event(row, "created", user)
                session.add(row)
                await session.flush()
                await session.refresh(row)
                data = _record(kind, row, live=r)
            return success_response(request=request, response=response, data=data,
                                    msg=f"Draft {row.reference_number} created",
                                    status_code=status.HTTP_201_CREATED)
        except assembly.NotFound as _ex:
            return not_found(request, response, _ex)
        except Exception as _ex:
            return error_response(request=request, exc=_ex, response=response)

    async def list_(request: Request, response: Response,
                    aircraft_id: Optional[int] = Query(None), policy_id: Optional[int] = Query(None),
                    registration: Optional[str] = Query(None, max_length=32),
                    status_: Optional[Literal["draft", "in_review", "approved", "issued"]] = Query(
                        None, alias="status"),
                    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        try:
            conds = []
            if aircraft_id is not None:
                conds.append(Model.aircraft_id == aircraft_id)
            if policy_id is not None:
                conds.append(Model.policy_id == policy_id)
            if status_:
                conds.append(Model.status == status_)
            if registration:
                key = "".join(ch for ch in registration.upper() if ch.isalnum())
                conds.append(func.upper(func.regexp_replace(Model.registration, "[^A-Za-z0-9]",
                                                            "", "g")) == key)
            stmt = select(Model).where(*conds).order_by(Model.created_at.desc(), Model.id.desc())
            async with request.app.state.db_client.read_session(DB) as session:
                rows, total = await page_with_total(
                    session, stmt, limit=limit, offset=offset,
                    count_stmt=select(func.count()).select_from(Model).where(*conds))
                items = [_record(kind, r, with_data=False) for r in rows]
            return success_response(request=request, response=response,
                                    data={"items": items, "total": total})
        except Exception as _ex:
            return error_response(request=request, exc=_ex, response=response)

    async def get_one(request: Request, response: Response, certificate_id: int):
        try:
            async with request.app.state.db_client.read_session(DB) as session:
                row = await _load(session, kind, certificate_id)
                if row is None:
                    return _not_found(request, response, kind, certificate_id)
                data = _record(kind, row, live=await _live(session, kind, row))
            return success_response(request=request, response=response, data=data)
        except assembly.NotFound as _ex:
            return not_found(request, response, _ex)
        except Exception as _ex:
            return error_response(request=request, exc=_ex, response=response)

    async def update(request: Request, response: Response, certificate_id: int, body: CertificatePatch,
                     token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
        try:
            async with request.app.state.db_client.session(DB) as session:
                await set_actor(session, token)
                row = await _load(session, kind, certificate_id, lock=True)
                if row is None:
                    return _not_found(request, response, kind, certificate_id)
                if row.status != DRAFT:
                    return _wrong_status(request, response, row, "draft")
                req = {**row.request, **_stored_inputs(body, exclude_unset=True)}
                # null drops an override; "" is kept (war_exclusion_exception: no exception)
                req = {k: v for k, v in req.items() if v is not None}
                r = await _resolve(session, kind, row.aircraft_id, req)
                _apply(row, kind, r, req)
                await session.flush()
                await session.refresh(row)
                data = _record(kind, row, live=r)
            return success_response(request=request, response=response, data=data)
        except assembly.NotFound as _ex:
            return not_found(request, response, _ex)
        except Exception as _ex:
            return error_response(request=request, exc=_ex, response=response)

    async def discard(request: Request, response: Response, certificate_id: int,
                      token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
        try:
            async with request.app.state.db_client.session(DB) as session:
                await set_actor(session, token)
                row = await _load(session, kind, certificate_id, lock=True)
                if row is None:
                    return _not_found(request, response, kind, certificate_id)
                if row.status != DRAFT:
                    return _wrong_status(request, response, row, "draft")
                reference = row.reference_number
                await session.delete(row)
            return success_response(request=request, response=response,
                                    data={"id": certificate_id, "reference_number": reference},
                                    msg=f"Draft {reference} discarded")
        except Exception as _ex:
            return error_response(request=request, exc=_ex, response=response)

    async def submit(request: Request, response: Response, certificate_id: int,
                     body: Optional[CommentIn] = None,
                     token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
        try:
            user = _portal_user()
            if user is None:
                return _no_user(request, response)
            async with request.app.state.db_client.session(DB) as session:
                await set_actor(session, token)
                row = await _load(session, kind, certificate_id, lock=True)
                if row is None:
                    return _not_found(request, response, kind, certificate_id)
                if row.status != DRAFT:
                    return _wrong_status(request, response, row, "draft")
                r = await _resolve(session, kind, row.aircraft_id, row.request)
                if r.errors:
                    return _incomplete(request, response, r.errors)
                _apply(row, kind, r, row.request)
                row.status, row.submitted_by_user, row.submitted_at = IN_REVIEW, user, _now()
                row.returned = None
                _event(row, "submitted", user, body.comment if body else None)
                await session.flush()
                await session.refresh(row)
                data = _record(kind, row, live=r)
            return success_response(request=request, response=response, data=data,
                                    msg=f"Certificate {row.reference_number} submitted for review")
        except assembly.NotFound as _ex:
            return not_found(request, response, _ex)
        except Exception as _ex:
            return error_response(request=request, exc=_ex, response=response)

    async def approve(request: Request, response: Response, certificate_id: int,
                      body: Optional[CommentIn] = None,
                      token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
        try:
            user = _portal_user()
            if user is None:
                return _no_user(request, response)
            async with request.app.state.db_client.session(DB) as session:
                await set_actor(session, token)
                row = await _load(session, kind, certificate_id, lock=True)
                if row is None:
                    return _not_found(request, response, kind, certificate_id)
                if row.status != IN_REVIEW:
                    return _wrong_status(request, response, row, "in_review")
                if _same(user, row.submitted_by_user):
                    return _fail(request, response, status.HTTP_409_CONFLICT, "same_person",
                                 "The certificate must be approved by someone other than who submitted it.")
                r = await _resolve(session, kind, row.aircraft_id, row.request)
                if r.errors:
                    return _incomplete(request, response, r.errors)
                _apply(row, kind, r, row.request)
                row.status, row.approved_by_user, row.approved_at = APPROVED, user, _now()
                row.approved_hash = signing.approved_hash(r.draft.data)
                _event(row, "approved", user, body.comment if body else None)
                await session.flush()
                await session.refresh(row)
                data = _record(kind, row, live=r)
            return success_response(request=request, response=response, data=data,
                                    msg=f"Certificate {row.reference_number} approved")
        except assembly.NotFound as _ex:
            return not_found(request, response, _ex)
        except Exception as _ex:
            return error_response(request=request, exc=_ex, response=response)

    async def return_(request: Request, response: Response, certificate_id: int, body: ReturnIn,
                      token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
        try:
            user = _portal_user()
            if user is None:
                return _no_user(request, response)
            async with request.app.state.db_client.session(DB) as session:
                await set_actor(session, token)
                row = await _load(session, kind, certificate_id, lock=True)
                if row is None:
                    return _not_found(request, response, kind, certificate_id)
                if row.status not in (IN_REVIEW, APPROVED):
                    return _wrong_status(request, response, row, "in_review or approved")
                at = _now()
                row.returned = {"by_user": user, "at": at.isoformat(), "comment": body.comment,
                                "from_status": row.status}
                row.status = DRAFT
                _clear_approval(row)
                _event(row, "returned", user, body.comment)
                await session.flush()
                await session.refresh(row)
                data = _record(kind, row, live=await _live(session, kind, row))
            return success_response(request=request, response=response, data=data,
                                    msg=f"Certificate {row.reference_number} returned to draft")
        except assembly.NotFound as _ex:
            return not_found(request, response, _ex)
        except Exception as _ex:
            return error_response(request=request, exc=_ex, response=response)

    async def prepare(request: Request, response: Response, certificate_id: int, body: PrepareIn,
                      token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
        try:
            user = _portal_user()
            if user is None:
                return _no_user(request, response)
            async with request.app.state.db_client.session(DB) as session:
                await set_actor(session, token)
                row = await _load(session, kind, certificate_id, lock=True, prep=True)
                if row is None:
                    return _not_found(request, response, kind, certificate_id)
                if row.status != APPROVED:
                    return _wrong_status(request, response, row, "approved")
                if _same(user, row.approved_by_user):
                    return _fail(request, response, status.HTTP_409_CONFLICT, "same_person",
                                 "The certificate must be signed by someone other than who approved it.")
                signatory = body.signatory.model_dump()
                r = await _resolve(session, kind, row.aircraft_id, row.request, signatory=signatory)
                if r.errors or signing.approved_hash(r.draft.data) != row.approved_hash:
                    # what the reviewer approved is not what would be signed: back to review
                    row.status = IN_REVIEW
                    _clear_approval(row)
                    return _fail(request, response, status.HTTP_409_CONFLICT, "changed_since_approval",
                                 "The certificate's values changed after it was approved; it is back "
                                 "in review.")
                _apply(row, kind, r, row.request)
                pdf = await run_in_threadpool(render, r.draft.data, reference_number=row.reference_number,
                                              date_of_issue=r.date_of_issue, images=r.images,
                                              signature_field=True)
                issue_token, token_hash, expires_at = signing.new_token()
                row.issue_prep = {
                    "token_hash": token_hash, "expires_at": expires_at.isoformat(),
                    "sha256": signing.sha256(pdf), "length": len(pdf),
                    "prepared_at": _now().isoformat(), "prepared_by_user": user,
                    "date_of_issue": r.date_of_issue.isoformat(),
                    "data": r.draft.data, "alerts": r.draft.alerts,
                }
            return success_response(request=request, response=response, data={
                "issue_token": issue_token, "expires_at": expires_at.isoformat(),
                "signature_field": SIGNATURE_FIELD, "pdf_base64": base64.b64encode(pdf).decode()},
                msg=f"Certificate {row.reference_number} prepared for signing")
        except assembly.NotFound as _ex:
            return not_found(request, response, _ex)
        except Exception as _ex:
            return error_response(request=request, exc=_ex, response=response)

    async def issue(request: Request, response: Response, certificate_id: int,
                    issue_token: Optional[str] = Form(None),
                    file: Optional[UploadFile] = File(None, description="The signed PDF."),
                    signature: Optional[str] = Form(None, description="JSON: the signature details."),
                    token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
        try:
            if not issue_token or file is None:
                return _fail(request, response, status.HTTP_409_CONFLICT, "signature_required",
                             "A certificate is issued by uploading the signed PDF: prepare it with "
                             "issue/prepare, sign the Signatory field, and send issue_token, file and "
                             "signature as multipart.")
            user = _portal_user()
            if user is None:
                return _no_user(request, response)
            try:
                sig = SignatureIn.model_validate(json.loads(signature or "{}"))
            except (ValueError, ValidationError) as ex:
                return _fail(request, response, status.HTTP_422_UNPROCESSABLE_ENTITY, "validation_error",
                             f"`signature` must be a JSON object of the signature details: {ex}")
            signed = await file.read(MAX_SIGNED_BYTES + 1)
            if len(signed) > MAX_SIGNED_BYTES:
                return _fail(request, response, status.HTTP_422_UNPROCESSABLE_ENTITY, "signature_invalid",
                             "The signed file is larger than 20 MB.")
            async with request.app.state.db_client.session(DB) as session:
                await set_actor(session, token)
                row = await _load(session, kind, certificate_id, lock=True, prep=True)
                if row is None:
                    return _not_found(request, response, kind, certificate_id)
                prep = row.issue_prep
                if not prep or not signing.token_matches(issue_token, prep["token_hash"]):
                    return _fail(request, response, status.HTTP_409_CONFLICT, "token_invalid",
                                 "The token is not the one handed out by the latest issue/prepare.")
                if prep.get("used_at") or row.status == ISSUED:
                    return _fail(request, response, status.HTTP_409_CONFLICT, "token_used",
                                 "This token was already used: the certificate is issued.")
                if row.status != APPROVED:
                    return _wrong_status(request, response, row, "approved")
                if datetime.fromisoformat(prep["expires_at"]) <= _now():
                    return _fail(request, response, status.HTTP_409_CONFLICT, "token_expired",
                                 "The token has expired: prepare the PDF again and sign the new one.")
                if _same(user, row.approved_by_user):
                    return _fail(request, response, status.HTTP_409_CONFLICT, "same_person",
                                 "The certificate must be signed by someone other than who approved it.")
                try:
                    await signing.verify_signed(signed, prepared_sha256=prep["sha256"],
                                                prepared_length=prep["length"])
                except signing.SignatureInvalid as ex:
                    return _fail(request, response, status.HTTP_422_UNPROCESSABLE_ENTITY,
                                 "signature_invalid", str(ex))
                at = _now()
                row.pdf = signed
                row.data, row.alerts, row.errors = prep["data"], prep["alerts"], []
                row.date_of_issue = date.fromisoformat(prep["date_of_issue"])
                row.status, row.issued_at = ISSUED, at
                row.issued_by = token.name if token is not None else "service-token"
                row.issued_by_user_id, row.issued_by_user_email, row.issued_by_user_name = (
                    user["id"], user["email"], user["name"])
                row.signature = {**sig.model_dump(mode="json"), "sha256": signing.sha256(signed)}
                row.issue_prep = {**prep, "used_at": at.isoformat()}
                _event(row, "issued", user)
                await session.flush()
                await session.refresh(row)
                data = _record(kind, row)
            return success_response(request=request, response=response, data=data,
                                    msg=f"Certificate {row.reference_number} issued")
        except Exception as _ex:
            return error_response(request=request, exc=_ex, response=response)

    async def pdf(request: Request, response: Response, certificate_id: int,
                  download: bool = Query(False)):
        try:
            async with request.app.state.db_client.read_session(DB) as session:
                row = await _load(session, kind, certificate_id)
                if row is None:
                    return _not_found(request, response, kind, certificate_id)
                if row.status == ISSUED:
                    # the bytes as signed: any re-render would break the signature
                    content = (await session.execute(select(Model.pdf).where(Model.id == row.id))
                               ).scalar_one()
                    suffix = ""
                else:
                    r = await _resolve(session, kind, row.aircraft_id, row.request)
                    if r.errors:
                        return _incomplete(request, response, r.errors)
                    content = await run_in_threadpool(render, r.draft.data,
                                                      reference_number=row.reference_number,
                                                      date_of_issue=r.date_of_issue, images=r.images,
                                                      draft=True)
                    suffix = "-DRAFT"
            name = f"{title}-Certificate-{row.reference_number.replace('/', '-')}{suffix}.pdf"
            return Response(content=bytes(content), media_type="application/pdf",
                            headers={"Content-Disposition":
                                     f'{"attachment" if download else "inline"}; filename="{name}"'})
        except assembly.NotFound as _ex:
            return not_found(request, response, _ex)
        except Exception as _ex:
            return error_response(request=request, exc=_ex, response=response)

    what = f"{kind} certificate"
    one = f"{base}/{{certificate_id}}"
    routes = (
        (f"{base}/preview", preview, ["POST"], None, _READ, "preview",
         f"What the {what} would say — `data`, `alerts`, `errors`, `can_submit` — without creating "
         f"anything. The reference is shown masked (CY25/SCAT/#####)."),
        (base, create, ["POST"], status.HTTP_201_CREATED, None, "create",
         f"Create a DRAFT {what}. Its reference number is allocated now and kept through every edit "
         f"(422 `incomplete` if the airline has no certificate code)."),
        (base, list_, ["GET"], None, _READ, "list",
         f"The {what}s, newest first. Filters: `aircraft_id`, `policy_id`, `registration` "
         f"(separator-insensitive), `status` (draft | in_review | approved | issued). "
         f"Returns `{{items, total}}`."),
        (one, get_one, ["GET"], None, _READ, "get",
         f"One {what}, with `data` and its trail `events`. Not yet issued: re-resolved from the current "
         f"policy, lease and aircraft; issued: exactly as signed."),
        (one, update, ["PATCH"], None, None, "update",
         f"Change a DRAFT's inputs — policy, date of issue, overrides, wording. Only the fields sent "
         f"change; null drops an override. 409 `wrong_status` unless draft."),
        (one, discard, ["DELETE"], None, None, "discard",
         f"Discard a DRAFT. 409 `wrong_status` unless draft. Its reference number is not reused."),
        (f"{one}/submit", submit, ["POST"], None, None, "submit",
         f"draft -> in_review. Body `{{comment?}}`. 422 `incomplete` while it has errors."),
        (f"{one}/approve", approve, ["POST"], None, None, "approve",
         f"in_review -> approved, recording the hash of the values approved. Body `{{comment?}}`. "
         f"409 `same_person` if the approver submitted it; 422 `incomplete` if errors appeared."),
        (f"{one}/return", return_, ["POST"], None, None, "return",
         f"in_review | approved -> draft. Body `{{comment}}` (1-2000 characters), kept in `returned`."),
        (f"{one}/issue/prepare", prepare, ["POST"], None, None, "issue_prepare",
         f"approved only. Body `{{signatory}}`. Draws the final PDF (no DRAFT mark, the signatory "
         f"printed, an empty signature field `Signatory`) and returns `{{issue_token, expires_at, "
         f"signature_field, pdf_base64}}`; the token is single use, lives 15 minutes, and a new prepare "
         f"replaces it. 409 `same_person` if the signer approved it; 409 `changed_since_approval` (and "
         f"back to in_review) if the values changed since approval."),
        (f"{one}/issue", issue, ["POST"], None, None, "issue",
         f"Issue by uploading the signed PDF: multipart `issue_token`, `file`, `signature` (JSON). The "
         f"file must be the prepared PDF plus a valid signature in `Signatory` (422 `signature_invalid`); "
         f"409 `token_invalid` / `token_used` / `token_expired` / `same_person`; without a file 409 "
         f"`signature_required`. The signed bytes become the issued PDF."),
        (f"{one}/pdf", pdf, ["GET"], None, _READ, "pdf",
         f"The PDF: not yet issued — drawn now and marked DRAFT on every page (422 `incomplete` while it "
         f"has errors); issued — the signed file, byte for byte. `download=true` asks the browser to "
         f"save it."),
    )
    for path, endpoint, methods, code, deps, name, description in routes:
        kwargs = {"methods": methods, "description": description, "dependencies": deps or [],
                  "operation_id": f"{kind}_certificate_{name}",
                  "responses": build_responses(include=_OK | ({code} if code else set()))}
        if code:
            kwargs["status_code"] = code
        router.add_api_route(path, endpoint, **kwargs)


# ==============================================================================================
# the issuing company — the portal's admin
# ==============================================================================================

def _image_meta(row: Optional[Asset], url: str) -> Optional[dict]:
    if row is None:
        return None
    return {"url": url, "content_type": row.content_type, "size": row.size, "sha256": row.sha256,
            "updated_at": iso(row.updated_at)}


async def _settings_payload(session) -> dict:
    values = await issuer_mod.load_settings(session)
    rows = {r.key: r for r in (await session.execute(
        select(Asset).where(Asset.key.in_(issuer_mod.COMPANY_ASSETS)))).scalars().all()}
    return {**values,
            "logo": _image_meta(rows.get("logo"), "/certificates/settings/logo"),
            "stamp": _image_meta(rows.get("stamp"), "/certificates/settings/stamp")}


def _company_asset(kind: str) -> str:
    if kind not in issuer_mod.COMPANY_ASSETS:
        raise RequestValidationError([{"loc": ("path", "kind"), "type": "value_error",
                                       "msg": "kind must be logo or stamp"}])
    return kind


@router.get(path="/settings",
            description="The issuing company as the certificates print it: names, address line, "
                        "legal footer, colours, the logo and stamp (metadata + URL), and the default "
                        "certificate wording (period wording, geographical limits, clauses).",
            responses=build_responses(include=_OK), dependencies=_READ)
async def get_settings(request: Request, response: Response):
    try:
        async with request.app.state.db_client.read_session(DB) as session:
            data = await _settings_payload(session)
        return success_response(request=request, response=response, data=data)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.patch(path="/settings",
              description="Change how the issuing company is printed and its default certificate "
                          "wording. Only the fields sent change; a wording field sent as null returns "
                          "to the market text. Issued certificates keep what they printed; drafts "
                          "pick it up at once.",
              responses=build_responses(include=_OK))
async def update_settings(request: Request, response: Response, body: SettingsPatch,
                          token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        fields = body.model_dump(exclude_unset=True)
        for key in ("company_name", "address_line", "legal_footer"):
            if key in fields and not (fields[key] or "").strip():
                fields[key] = None
        for key in ("company_legal_name", "brand_primary", "brand_accent"):
            if key in fields and fields[key] is None:
                fields.pop(key)
        for key in WORDING_FIELDS:
            if key in fields and fields[key] is None:
                fields[key] = MARKET_WORDING[key]
        async with request.app.state.db_client.session(DB) as session:
            await set_actor(session, token)
            row = (await session.execute(select(Settings).where(Settings.id == 1))).scalar_one_or_none()
            if row is None:
                row = Settings(id=1, **{**issuer_mod.DEFAULTS, **fields})
                session.add(row)
            else:
                for key, value in fields.items():
                    setattr(row, key, value.strip() if isinstance(value, str) else value)
            await session.flush()
            data = await _settings_payload(session)
        return success_response(request=request, response=response, data=data)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.put(path="/settings/{kind}",
            description="Upload the company `logo` (top right of every page) or `stamp` (beside the "
                        "signature; printed only when uploaded): multipart field `file`, PNG, JPEG or "
                        "SVG (drawn as vector), up to 2 MB. Replaces the current one.",
            responses=build_responses(include=_OK))
async def upload_company_image(request: Request, response: Response, kind: str,
                               file: UploadFile = File(...),
                               token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        key = _company_asset(kind)
        data = await file.read(issuer_mod.MAX_IMAGE_BYTES + 1)
        try:
            content_type = await run_in_threadpool(issuer_mod.validate_image, file.content_type, data)
        except issuer_mod.InvalidImage as ex:
            raise RequestValidationError([{"loc": ("body", "file"), "msg": str(ex), "type": "value_error"}])
        _kind, user = current_actor()
        async with request.app.state.db_client.session(DB) as session:
            await set_actor(session, token)
            row = (await session.execute(select(Asset).where(Asset.key == key))).scalar_one_or_none()
            if row is None:
                row = Asset(key=key)
                session.add(row)
            row.content_type, row.data = content_type, data
            row.sha256, row.size = issuer_mod.sha256(data), len(data)
            row.updated_by = token.name if token is not None else "service-token"
            row.updated_by_user_id = user.id if user else None
            await session.flush()
            meta = {"content_type": row.content_type, "size": row.size, "sha256": row.sha256,
                    "url": f"/certificates/settings/{kind}"}
        return success_response(request=request, response=response, data=meta,
                                msg=f"{kind.capitalize()} saved")
    except RequestValidationError:
        raise
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.get(path="/settings/{kind}",
            description="The company `logo` or `stamp` image itself (its own content type).",
            responses=build_responses(include=_OK), dependencies=_READ)
async def get_company_image(request: Request, response: Response, kind: str):
    try:
        key = _company_asset(kind)
        async with request.app.state.db_client.read_session(DB) as session:
            found = (await session.execute(
                select(Asset.content_type, Asset.data, Asset.sha256).where(Asset.key == key))).first()
        if found is None:
            return warning_response(request=request, response=response, msg=f"No {kind} is set",
                                    status_code=status.HTTP_404_NOT_FOUND)
        return Response(content=bytes(found.data), media_type=found.content_type,
                        headers={"Cache-Control": "no-cache", "ETag": f'"{found.sha256}"'})
    except RequestValidationError:
        raise
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.delete(path="/settings/{kind}",
               description="Remove the company `logo` or `stamp`; certificates are then drawn without it.",
               responses=build_responses(include=_OK))
async def delete_company_image(request: Request, response: Response, kind: str,
                               token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        key = _company_asset(kind)
        async with request.app.state.db_client.session(DB) as session:
            await set_actor(session, token)
            removed = (await session.execute(delete(Asset).where(Asset.key == key))).rowcount > 0
        if not removed:
            return warning_response(request=request, response=response, msg=f"No {kind} is set",
                                    status_code=status.HTTP_404_NOT_FOUND)
        return success_response(request=request, response=response, data={"kind": kind},
                                msg=f"{kind.capitalize()} removed")
    except RequestValidationError:
        raise
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


# The settings routes are registered first: `/certificates/settings` must never be read as a kind.
for _kind in assembly.KINDS:
    _register(_kind)
