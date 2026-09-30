"""Certificates for insured aircraft (AVN 67B): `/certificates/reinsurance` and `/certificates/insurance`.

Both documents share one flow, one set of endpoints per kind:

    POST   /certificates/{kind}/preview      what it would say — values, alerts, errors — nothing saved
    POST   /certificates/{kind}              create a DRAFT; its reference number is allocated now
    GET    /certificates/{kind}              the certificates of that kind, newest first
    GET    /certificates/{kind}/{id}         one — a draft re-resolved live, an issued one as frozen
    PATCH  /certificates/{kind}/{id}         change a draft's inputs (409 once issued)
    POST   /certificates/{kind}/{id}/issue   issue it: freeze values, alerts and the PDF (409 once issued)
    DELETE /certificates/{kind}/{id}         discard a draft (409 once issued; the number is not reused)
    GET    /certificates/{kind}/{id}/pdf     a draft drawn live and marked DRAFT; an issued one as sent

    GET/PATCH      /certificates/settings                the issuing company as printed, and its default
                                                          certificate wording (admin)
    PUT/GET/DELETE /certificates/settings/{logo|stamp}   its images (admin)

DRAFT vs ISSUED. A draft is a living document: its values are resolved from the policy, the lease and
the aircraft whenever it is read or drawn, and every PDF of it says DRAFT on every page. Issuing it is
final — the database refuses any later change to the row.

THE SIGNATORY is the portal user issuing the certificate, who signs the printed copy by hand. The
portal sends `signatory: {full_name, title, phone, email}` with the request; without it the name and
e-mail of the X-Portal-User-* headers are used, and with neither the certificate cannot be issued.
"""
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Literal, Optional

from fastapi import Depends, File, Query, Request, Response, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from Certificates import assemble as assembly, issuer as issuer_mod, numbering
from Certificates.render import render
from Config import setup_logger
from Database import ApiToken
from Database.CertificateModels import (DRAFT, ISSUED, MARKET_WORDING, WORDING_FIELDS, Asset,
                                        InsuranceCertificate, ReinsuranceCertificate, Settings)
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


# ==============================================================================================
# bodies
# ==============================================================================================

class Addressee(BaseModel):
    company: str = Field(min_length=1, max_length=256)
    contacts: Optional[str] = Field(default=None, max_length=256)
    email: Optional[str] = Field(default=None, max_length=256)


class SignatoryIn(BaseModel):
    """Who signs the printed certificate by hand — the portal user issuing it."""
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
    wording) applies again. `war_exclusion_exception: ""` is an override ("no exception"), not null."""
    policy_id: Optional[int] = Field(
        default=None, description="Default: the policy covering the aircraft on the date of issue, "
                                  "else the next one to start.")
    date_of_issue: Optional[date] = Field(
        default=None, description="Default: the day it is issued (system-generated).")
    signed_by: Optional[Literal["broker", "insurer"]] = Field(
        default=None, description="Insurance certificate only: who signs it — `broker` (us, as the "
                                  "Insured's insurance broker; default) or `insurer`.")
    signatory: Optional[SignatoryIn] = Field(
        default=None, description="The signatory printed on it. Default: the portal user's name and "
                                  "e-mail from the X-Portal-User-* headers.")
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


async def _resolve(session, kind: str, aircraft_id: int, req: dict, *, issuing_on: Optional[date] = None):
    """The certificate as its inputs make it today: values, alerts, errors, the issuer and the
    signatory, and the images to draw it with."""
    date_of_issue = (date.fromisoformat(req["date_of_issue"]) if req.get("date_of_issue")
                     else issuing_on or date.today())
    issuer, company_wording, images = await issuer_mod.load(session)
    draft = await assembly.assemble(
        session, kind, aircraft_id=aircraft_id, date_of_issue=date_of_issue,
        policy_id=req.get("policy_id"), overrides=_overrides(req),
        signed_by=req.get("signed_by") or "broker",
        wording=issuer_mod.resolve_wording(req, company_wording))
    draft.data["issuer"] = issuer
    signatory = req.get("signatory")
    if not signatory:
        _kind, user = current_actor()
        signatory = ({"full_name": user.name, "title": None, "phone": None, "email": user.email}
                     if user is not None and user.name else None)
    draft.data["signatory"] = signatory
    errors = list(draft.errors)
    if not signatory or not signatory.get("full_name"):
        errors.append({"field": "signatory", "msg": "No signatory: send `signatory.full_name` (or "
                                                     "call as a portal user)."})
    return _Resolved(draft, images, date_of_issue, errors)


def _errors_out(errors) -> list:
    return [{"field": f"certificate.{e['field']}", "msg": e["msg"]} for e in errors]


def _as_422(errors) -> RequestValidationError:
    return RequestValidationError([{"loc": ("certificate", e["field"]), "msg": e["msg"],
                                    "type": "value_error"} for e in errors])


_NUMBERING_FIELDS = {"airline", "certificate_code"}


def _record(kind: str, row, *, live: Optional[_Resolved] = None, with_data: bool = True) -> dict:
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
        "errors": _errors_out(live.errors) if live else _errors_out(row.errors),
        "can_issue": row.status == DRAFT and not (live.errors if live else row.errors),
        "created_by_user": ({"id": row.created_by_user_id, "name": row.created_by_user_name}
                            if row.created_by_user_id or row.created_by_user_name else None),
        "created_at": iso(row.created_at),
        "updated_at": iso(row.updated_at),
        "issued_at": iso(row.issued_at),
        "issued_by": row.issued_by,
        "issued_by_user": ({"id": row.issued_by_user_id, "email": row.issued_by_user_email,
                            "name": row.issued_by_user_name}
                           if row.issued_by_user_id or row.issued_by_user_name else None),
        "request": row.request,
        "pdf_url": f"/certificates/{kind}/{row.id}/pdf",
    }
    if with_data:
        out["data"] = live.draft.data if live else row.data
    return out


def _apply(row, kind: str, resolved: _Resolved, req: dict) -> None:
    """Write a resolution onto a draft row. The sequence number stays; the reference is rebuilt in
    case the policy (contract year) or the airline code changed."""
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


async def _load(session, kind: str, certificate_id: int, *, lock: bool = False):
    stmt = select(MODELS[kind]).where(MODELS[kind].id == certificate_id)
    if lock:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


def _not_found(request, response, kind, certificate_id):
    return warning_response(request=request, response=response,
                            msg=f"{_TITLE[kind]} certificate {certificate_id} not found",
                            status_code=status.HTTP_404_NOT_FOUND)


def _is_issued(request, response, row):
    return warning_response(request=request, response=response,
                            msg=f"Certificate {row.reference_number} is issued and cannot be changed.",
                            status_code=status.HTTP_409_CONFLICT)


# ==============================================================================================
# the endpoints, once per kind
# ==============================================================================================

def _register(kind: str) -> None:
    Model = MODELS[kind]
    title = _TITLE[kind]
    base = f"/{kind}"

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
                "errors": _errors_out(r.errors), "can_issue": not r.errors})
        except assembly.NotFound as _ex:
            return warning_response(request=request, response=response, msg=str(_ex),
                                    status_code=status.HTTP_404_NOT_FOUND)
        except Exception as _ex:
            return error_response(request=request, exc=_ex, response=response)

    async def create(request: Request, response: Response, body: CertificateIn,
                     token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
        try:
            req = _stored_inputs(body, exclude_unset=True)
            _kind, user = current_actor()
            if not req.get("signatory") and user is not None and user.name:
                # kept with the draft, so it prints the same whoever opens it later
                req["signatory"] = {"full_name": user.name, "title": None, "phone": None,
                                    "email": user.email}
            async with request.app.state.db_client.session(DB) as session:
                await set_actor(session, token)
                r = await _resolve(session, kind, body.aircraft_id, req)
                blocking = [e for e in r.errors if e["field"] in _NUMBERING_FIELDS]
                if blocking:   # without an airline code there is no reference number to give it
                    raise _as_422(blocking)
                scope, sequence = await numbering.next_sequence(session, kind)
                row = Model(status=DRAFT, sequence_no=sequence, counter_scope=scope,
                            aircraft_id=body.aircraft_id,
                            created_by_user_id=user.id if user else None,
                            created_by_user_name=user.name if user else None)
                _apply(row, kind, r, req)
                session.add(row)
                await session.flush()
                await session.refresh(row)
                data = _record(kind, row, live=r)
            return success_response(request=request, response=response, data=data,
                                    msg=f"Draft {row.reference_number} created",
                                    status_code=status.HTTP_201_CREATED)
        except RequestValidationError:
            raise
        except assembly.NotFound as _ex:
            return warning_response(request=request, response=response, msg=str(_ex),
                                    status_code=status.HTTP_404_NOT_FOUND)
        except Exception as _ex:
            return error_response(request=request, exc=_ex, response=response)

    async def list_(request: Request, response: Response,
                    aircraft_id: Optional[int] = Query(None), policy_id: Optional[int] = Query(None),
                    registration: Optional[str] = Query(None, max_length=32),
                    status_: Optional[Literal["draft", "issued"]] = Query(None, alias="status"),
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
                live = None
                if row.status == DRAFT and row.aircraft_id is not None:
                    live = await _resolve(session, kind, row.aircraft_id, row.request)
                data = _record(kind, row, live=live)
            return success_response(request=request, response=response, data=data)
        except assembly.NotFound as _ex:
            return warning_response(request=request, response=response, msg=str(_ex),
                                    status_code=status.HTTP_404_NOT_FOUND)
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
                if row.status == ISSUED:
                    return _is_issued(request, response, row)
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
            return warning_response(request=request, response=response, msg=str(_ex),
                                    status_code=status.HTTP_404_NOT_FOUND)
        except Exception as _ex:
            return error_response(request=request, exc=_ex, response=response)

    async def issue(request: Request, response: Response, certificate_id: int,
                    token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
        try:
            _kind, user = current_actor()
            async with request.app.state.db_client.session(DB) as session:
                await set_actor(session, token)
                row = await _load(session, kind, certificate_id, lock=True)
                if row is None:
                    return _not_found(request, response, kind, certificate_id)
                if row.status == ISSUED:
                    return _is_issued(request, response, row)
                r = await _resolve(session, kind, row.aircraft_id, row.request, issuing_on=date.today())
                if r.errors:
                    raise _as_422(r.errors)
                _apply(row, kind, r, row.request)
                row.pdf = await run_in_threadpool(render, r.draft.data,
                                                  reference_number=row.reference_number,
                                                  date_of_issue=r.date_of_issue, images=r.images)
                row.status, row.issued_at, row.errors = ISSUED, datetime.now(timezone.utc), []
                row.issued_by = token.name if token is not None else "service-token"
                row.issued_by_user_id = user.id if user else None
                row.issued_by_user_email = user.email if user else None
                row.issued_by_user_name = user.name if user else None
                await session.flush()
                await session.refresh(row)
                data = _record(kind, row)
            return success_response(request=request, response=response, data=data,
                                    msg=f"Certificate {row.reference_number} issued")
        except RequestValidationError:
            raise
        except assembly.NotFound as _ex:
            return warning_response(request=request, response=response, msg=str(_ex),
                                    status_code=status.HTTP_404_NOT_FOUND)
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
                if row.status == ISSUED:
                    return _is_issued(request, response, row)
                reference = row.reference_number
                await session.delete(row)
            return success_response(request=request, response=response,
                                    data={"id": certificate_id, "reference_number": reference},
                                    msg=f"Draft {reference} discarded")
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
                    content = (await session.execute(select(Model.pdf).where(Model.id == row.id))
                               ).scalar_one()
                    suffix = ""
                else:
                    r = await _resolve(session, kind, row.aircraft_id, row.request)
                    if r.errors:
                        raise _as_422(r.errors)
                    content = await run_in_threadpool(render, r.draft.data,
                                                      reference_number=row.reference_number,
                                                      date_of_issue=r.date_of_issue, images=r.images,
                                                      draft=True)
                    suffix = "-DRAFT"
            name = f"{title}-Certificate-{row.reference_number.replace('/', '-')}{suffix}.pdf"
            return Response(content=bytes(content), media_type="application/pdf",
                            headers={"Content-Disposition":
                                     f'{"attachment" if download else "inline"}; filename="{name}"'})
        except RequestValidationError:
            raise
        except assembly.NotFound as _ex:
            return warning_response(request=request, response=response, msg=str(_ex),
                                    status_code=status.HTTP_404_NOT_FOUND)
        except Exception as _ex:
            return error_response(request=request, exc=_ex, response=response)

    what = f"{kind} certificate"
    routes = (
        (f"{base}/preview", preview, ["POST"], None, _READ,
         f"What the {what} would say — `data`, `alerts`, `errors`, `can_issue` — without creating "
         f"anything. The reference is shown masked (CY25/SCAT/#####)."),
        (base, create, ["POST"], status.HTTP_201_CREATED, None,
         f"Create a DRAFT {what}. Its reference number is allocated now and kept through every edit "
         f"(422 if the airline has no certificate code). A draft may still have `errors`; it can be "
         f"edited, drawn (marked DRAFT) and issued once they are resolved."),
        (base, list_, ["GET"], None, _READ,
         f"The {what}s, newest first. Filters: `aircraft_id`, `policy_id`, `registration` "
         f"(separator-insensitive), `status` (draft | issued). Returns `{{items, total}}`."),
        (f"{base}/{{certificate_id}}", get_one, ["GET"], None, _READ,
         f"One {what}. A draft is re-resolved from the current policy, lease and aircraft; an issued "
         f"one is returned exactly as issued."),
        (f"{base}/{{certificate_id}}", update, ["PATCH"], None, None,
         f"Change a DRAFT's inputs — policy, date of issue, signatory, overrides. Only the fields sent "
         f"change; null drops an override. 409 once issued."),
        (f"{base}/{{certificate_id}}/issue", issue, ["POST"], None, None,
         f"Issue the draft: resolve it one last time, refuse with 422 while anything required is "
         f"missing (`certificate.<field>`), then freeze its values, alerts and the PDF. A date of "
         f"issue left to the system becomes today. Final: no later change is accepted (409)."),
        (f"{base}/{{certificate_id}}", discard, ["DELETE"], None, None,
         f"Discard a DRAFT. 409 once issued. Its reference number is not reused."),
        (f"{base}/{{certificate_id}}/pdf", pdf, ["GET"], None, _READ,
         f"The PDF: a draft drawn now and marked DRAFT on every page (422 while it has errors), an "
         f"issued one exactly as sent. `download=true` asks the browser to save it."),
    )
    for path, endpoint, methods, code, deps, description in routes:
        kwargs = {"methods": methods, "description": description, "dependencies": deps or [],
                  "operation_id": f"{kind}_certificate_{endpoint.__name__.rstrip('_')}",
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
