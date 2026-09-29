"""Certificates for insured aircraft: `/certificates/reinsurance` (AVN 67B).

    POST /certificates/reinsurance/preview   what the certificate would say — values, alerts, errors —
                                             without numbering or saving anything
    POST /certificates/reinsurance           issue it: number it, draw the PDF, keep both in history
    GET  /certificates/reinsurance           the history, newest first
    GET  /certificates/reinsurance/{id}      one issued certificate with every value it printed
    GET  /certificates/reinsurance/{id}/pdf  the PDF exactly as issued
    POST /certificates/reinsurance/preview/pdf  the document as it would be issued, marked DRAFT

    GET/PATCH  /certificates/settings                 the issuing company as printed
    PUT/GET/DELETE /certificates/settings/{logo|stamp}  its images (PNG, JPEG or SVG)
    GET/PATCH  /certificates/signatory                the calling portal user's signatory details
    PUT/GET/DELETE /certificates/signatory/signature  their signature image

THE SIGNATORY IS THE PORTAL USER WHO ISSUES THE CERTIFICATE. Their name and e-mail come with the
request (X-Portal-User-*, believed only with the service token); title, phone and signature are what
they keep at /certificates/signatory. A certificate cannot be issued without a portal user.

A certificate is issued for an INSURED aircraft only — the policy is the one named, else the one
covering it on the date of issue, else the next one to start. The values come from the policy, the
aircraft and the lease (Certificates/reinsurance.py says which from where); what an e-mail, rider or
mark-up says instead is sent with the request and kept with the certificate.

The history is append-only: a correction is a new certificate with a new number, never an edit.
"""
from datetime import date
from decimal import Decimal
from typing import Optional

from fastapi import Depends, File, Query, Request, Response, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from Certificates import issuer as issuer_mod, numbering, reinsurance
from Certificates.render import render
from Config import setup_logger
from Database import ApiToken
from Database.CertificateModels import Asset, ReinsuranceCertificate, Settings, Signatory
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


# ==============================================================================================
# bodies
# ==============================================================================================

class Addressee(BaseModel):
    company: str = Field(min_length=1, max_length=256)
    contacts: Optional[str] = Field(default=None, max_length=256)
    email: Optional[str] = Field(default=None, max_length=256)


class ReinsuranceRequest(BaseModel):
    """Which aircraft, as of when — and what an e-mail, rider or mark-up says instead of the stored
    value. Everything but `aircraft_id` is optional."""
    aircraft_id: int
    policy_id: Optional[int] = Field(
        default=None, description="Default: the policy covering the aircraft on the date of issue, "
                                  "else the next one to start.")
    date_of_issue: Optional[date] = Field(
        default=None, description="Default: today (system-generated). Sent = as requested.")
    agreed_value: Optional[Decimal] = Field(
        default=None, ge=0, description="In place of the lease's agreed value (A10).")
    equipment: Optional[str] = Field(
        default=None, max_length=256,
        description="In place of '<manufacturer> <series>' (A7) — e.g. to name the original engines.")
    effective_date: Optional[date] = Field(
        default=None, description="In place of the lease terms' effective date (A28).")
    contract_parties: Optional[list[str]] = Field(
        default=None, min_length=1, max_length=20,
        description="In place of the agreement's contract parties (A24), in order.")
    contracts: Optional[list[str]] = Field(
        default=None, min_length=1, max_length=20,
        description="In place of the contracts list (A25-A27): the full text of each item.")
    addressees: Optional[list[Addressee]] = Field(
        default=None, max_length=20,
        description="In place of the contract parties' contacts on the Schedule of Parties (A29).")

    def overrides(self) -> reinsurance.Overrides:
        return reinsurance.Overrides(
            agreed_value=self.agreed_value, equipment=self.equipment,
            effective_date=self.effective_date, contract_parties=self.contract_parties,
            contracts=self.contracts,
            addressees=[a.model_dump() for a in self.addressees] if self.addressees is not None else None)


class SettingsPatch(BaseModel):
    """The issuing company as the certificates print it. Only the fields sent change; send
    `company_name`, `address_line` or `legal_footer` as null (or "") to leave them off."""
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


class SignatoryPatch(BaseModel):
    """What the certificate prints under your signature. `name` / `email` override the portal
    profile's; null (or "") falls back to it."""
    title: Optional[str] = Field(default=None, max_length=128, description="e.g. Head of Insurance.")
    phone: Optional[str] = Field(default=None, max_length=64)
    name: Optional[str] = Field(default=None, max_length=256)
    email: Optional[str] = Field(default=None, max_length=256)


# ==============================================================================================
# helpers
# ==============================================================================================

def _summary(row: ReinsuranceCertificate) -> dict:
    return {
        "id": row.id,
        "reference_number": row.reference_number,
        "date_of_issue": iso(row.date_of_issue),
        "date_of_issue_source": row.date_of_issue_source,
        "variant": row.variant,
        "registration": row.registration,
        "msn": row.msn,
        "aircraft_id": row.aircraft_id,
        "policy_id": row.policy_id,
        "aircraft_lease_id": row.aircraft_lease_id,
        "alerts": row.alerts,
        "issued_by": row.issued_by,
        "issued_by_user": ({"id": row.issued_by_user_id, "email": row.issued_by_user_email,
                            "name": row.issued_by_user_name}
                           if row.issued_by_user_id or row.issued_by_user_name else None),
        "created_at": iso(row.created_at),
        "pdf_url": f"/certificates/reinsurance/{row.id}/pdf",
    }


def _data_errors(draft) -> RequestValidationError:
    return RequestValidationError([{"loc": ("certificate", e["field"]), "msg": e["msg"],
                                    "type": "value_error"} for e in draft.errors])


def _no_signatory() -> RequestValidationError:
    return RequestValidationError([{
        "loc": ("certificate", "signatory"), "type": "value_error",
        "msg": "A certificate is signed by the portal user who issues it, and this request names none "
               "(X-Portal-User-* with the service token)."}])


def _need_user():
    """The calling portal user, or a 422 — the signatory endpoints are about 'me'."""
    _kind, user = current_actor()
    if user is None:
        raise RequestValidationError([{
            "loc": ("header", "X-Portal-User-Id"), "type": "value_error",
            "msg": "This endpoint is about the calling portal user; send X-Portal-User-* with the "
                   "service token."}])
    return user


def _image_meta(row: Optional[Asset], url: str) -> Optional[dict]:
    if row is None:
        return None
    return {"url": url, "content_type": row.content_type, "size": row.size, "sha256": row.sha256,
            "updated_at": iso(row.updated_at)}


async def _upload(request, key: str, file: UploadFile, token) -> dict:
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
        return {"content_type": row.content_type, "size": row.size, "sha256": row.sha256}


async def _serve(request, key: str) -> Response:
    async with request.app.state.db_client.read_session(DB) as session:
        found = (await session.execute(
            select(Asset.content_type, Asset.data, Asset.sha256).where(Asset.key == key))).first()
    if found is None:
        return None
    return Response(content=bytes(found.data), media_type=found.content_type,
                    headers={"Cache-Control": "no-cache", "ETag": f'"{found.sha256}"'})


async def _remove(request, key: str, token) -> bool:
    async with request.app.state.db_client.session(DB) as session:
        await set_actor(session, token)
        return (await session.execute(delete(Asset).where(Asset.key == key))).rowcount > 0


def _preview_reference(draft) -> Optional[str]:
    if draft.airline_code is None or draft.contract_year is None:
        return None
    return numbering.reference_number(draft.contract_year, draft.airline_code, 0).replace(
        "00000", "#####")


# ==============================================================================================
# reinsurance
# ==============================================================================================

@router.post(
    path="/reinsurance/preview",
    description=(
        "What the reinsurance certificate would say, without issuing it: every value it would print "
        "(`data`), the `alerts` a person should look at (a lease limit above the policy's, a "
        "contract party without a notice address, …) and the `errors` that would stop it being "
        "issued (a value the document needs and nobody has recorded). The reference number is shown "
        "with its sequence masked — it is allocated only on issue."
    ),
    responses=build_responses(include=_OK), dependencies=_READ,
)
async def preview_reinsurance(request: Request, response: Response, body: ReinsuranceRequest):
    try:
        issued_on = body.date_of_issue or date.today()
        _kind, user = current_actor()
        async with request.app.state.db_client.read_session(DB) as session:
            draft = await reinsurance.assemble(
                session, aircraft_id=body.aircraft_id, date_of_issue=issued_on,
                policy_id=body.policy_id, overrides=body.overrides())
            draft.data["issuer"], _images = await issuer_mod.load(session, user)
        errors = list(draft.errors)
        if user is None:
            errors.append({"field": "signatory", "msg": "No portal user: the certificate is signed "
                                                        "by the user who issues it."})
        return success_response(request=request, response=response, data={
            "reference_number": _preview_reference(draft),
            "date_of_issue": issued_on.isoformat(),
            "variant": draft.variant,
            "data": draft.data,
            "alerts": draft.alerts,
            "errors": [{"field": f"certificate.{e['field']}", "msg": e["msg"]} for e in errors],
            "can_issue": not errors,
        })
    except reinsurance.NotFound as _ex:
        return warning_response(request=request, response=response, msg=str(_ex),
                                status_code=status.HTTP_404_NOT_FOUND)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.post(
    path="/reinsurance",
    description=(
        "Issue the reinsurance certificate, signed by the calling PORTAL USER (X-Portal-User-* with "
        "the service token; an API key or no user is a 422 `certificate.signatory`): resolve every "
        "value (as the preview does), refuse with 422 if any the document needs is missing "
        "(`field` = `certificate.<name>`), allocate the "
        "reference number CY<yy>/<airline code>/<nnnnn>, draw the PDF and keep the values, the "
        "alerts and the PDF in the history. `date_of_issue` defaults to today. Returns the record, "
        "with `pdf_url`."
    ),
    status_code=status.HTTP_201_CREATED,
    responses=build_responses(include=_OK | {status.HTTP_201_CREATED}),
)
async def issue_reinsurance(request: Request, response: Response, body: ReinsuranceRequest,
                            token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        issued_on = body.date_of_issue or date.today()
        _kind, user = current_actor()
        if token is not None or user is None:
            raise _no_signatory()
        async with request.app.state.db_client.session(DB) as session:
            await set_actor(session, token)
            draft = await reinsurance.assemble(
                session, aircraft_id=body.aircraft_id, date_of_issue=issued_on,
                policy_id=body.policy_id, overrides=body.overrides())
            if draft.errors:
                raise _data_errors(draft)
            draft.data["issuer"], images = await issuer_mod.load(session, user)
            if not draft.data["issuer"]["signatory"]["name"]:
                raise _no_signatory()
            scope, sequence = await numbering.next_sequence(session, reinsurance.KIND)
            reference = numbering.reference_number(draft.contract_year, draft.airline_code, sequence)
            # CPU work, a few tens of milliseconds: off the event loop
            pdf = await run_in_threadpool(render, draft.data, reference_number=reference,
                                          date_of_issue=issued_on, images=images)
            row = ReinsuranceCertificate(
                reference_number=reference, contract_year=draft.contract_year,
                airline_code=draft.airline_code, sequence_no=sequence, counter_scope=scope,
                date_of_issue=issued_on,
                date_of_issue_source="user" if body.date_of_issue else "system",
                variant=draft.variant, template_version=reinsurance.TEMPLATE_VERSION,
                registration=draft.registration, msn=draft.msn,
                data=draft.data, alerts=draft.alerts, pdf=pdf,
                issued_by=token.name if token is not None else "service-token",
                issued_by_user_id=user.id, issued_by_user_email=user.email,
                issued_by_user_name=user.name,
                aircraft_id=draft.aircraft_id, policy_id=draft.policy_id,
                aircraft_lease_id=draft.aircraft_lease_id)
            session.add(row)
            await session.flush()
            data = _summary(row) | {"data": row.data}
        return success_response(request=request, response=response, data=data,
                                msg=f"Certificate {reference} issued",
                                status_code=status.HTTP_201_CREATED)
    except RequestValidationError:
        raise
    except reinsurance.NotFound as _ex:
        return warning_response(request=request, response=response, msg=str(_ex),
                                status_code=status.HTTP_404_NOT_FOUND)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.get(
    path="/reinsurance",
    description=("Every reinsurance certificate issued, newest first. `aircraft_id`, `policy_id` and "
                 "`registration` (separator-insensitive) narrow it. Returns `{items, total}`; the "
                 "values and the PDF are on the single-certificate endpoints."),
    responses=build_responses(include=_OK), dependencies=_READ,
)
async def list_reinsurance(request: Request, response: Response,
                           aircraft_id: Optional[int] = Query(None),
                           policy_id: Optional[int] = Query(None),
                           registration: Optional[str] = Query(None, max_length=32),
                           limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    try:
        conds = []
        if aircraft_id is not None:
            conds.append(ReinsuranceCertificate.aircraft_id == aircraft_id)
        if policy_id is not None:
            conds.append(ReinsuranceCertificate.policy_id == policy_id)
        if registration:
            key = "".join(ch for ch in registration.upper() if ch.isalnum())
            conds.append(func.upper(func.regexp_replace(ReinsuranceCertificate.registration,
                                                        "[^A-Za-z0-9]", "", "g")) == key)
        stmt = (select(ReinsuranceCertificate).where(*conds)
                .order_by(ReinsuranceCertificate.created_at.desc(), ReinsuranceCertificate.id.desc()))
        async with request.app.state.db_client.read_session(DB) as session:
            rows, total = await page_with_total(
                session, stmt, limit=limit, offset=offset,
                count_stmt=select(func.count()).select_from(ReinsuranceCertificate).where(*conds))
            items = [_summary(r) for r in rows]
        return success_response(request=request, response=response,
                                data={"items": items, "total": total})
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.get(path="/reinsurance/{certificate_id}",
            description="One issued reinsurance certificate, with every value it printed (`data`).",
            responses=build_responses(include=_OK), dependencies=_READ)
async def get_reinsurance(request: Request, response: Response, certificate_id: int):
    try:
        async with request.app.state.db_client.read_session(DB) as session:
            row = (await session.execute(select(ReinsuranceCertificate)
                                         .where(ReinsuranceCertificate.id == certificate_id))
                   ).scalar_one_or_none()
            if row is None:
                return warning_response(request=request, response=response,
                                        msg=f"Certificate {certificate_id} not found",
                                        status_code=status.HTTP_404_NOT_FOUND)
            data = _summary(row) | {"data": row.data}
        return success_response(request=request, response=response, data=data)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.get(path="/reinsurance/{certificate_id}/pdf",
            description="The PDF of an issued reinsurance certificate, exactly as issued "
                        "(application/pdf, inline). `download=true` asks the browser to save it.",
            responses=build_responses(include=_OK), dependencies=_READ)
async def reinsurance_pdf(request: Request, response: Response, certificate_id: int,
                          download: bool = Query(False)):
    try:
        async with request.app.state.db_client.read_session(DB) as session:
            found = (await session.execute(
                select(ReinsuranceCertificate.reference_number, ReinsuranceCertificate.pdf)
                .where(ReinsuranceCertificate.id == certificate_id))).first()
        if found is None:
            return warning_response(request=request, response=response,
                                    msg=f"Certificate {certificate_id} not found",
                                    status_code=status.HTTP_404_NOT_FOUND)
        name = "Reinsurance-Certificate-" + found.reference_number.replace("/", "-") + ".pdf"
        disposition = "attachment" if download else "inline"
        return Response(content=bytes(found.pdf), media_type="application/pdf",
                        headers={"Content-Disposition": f'{disposition}; filename="{name}"'})
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.post(
    path="/reinsurance/preview/pdf",
    description=(
        "The certificate as it would be issued now, as a PDF marked DRAFT across every page, with the "
        "reference masked (CY25/SCAT/#####). Nothing is numbered or saved. The same body as the "
        "preview; 422 with `certificate.<field>` entries while a value the document needs is missing."
    ),
    responses=build_responses(include=_OK), dependencies=_READ,
)
async def preview_reinsurance_pdf(request: Request, response: Response, body: ReinsuranceRequest):
    try:
        issued_on = body.date_of_issue or date.today()
        _kind, user = current_actor()
        async with request.app.state.db_client.read_session(DB) as session:
            draft = await reinsurance.assemble(
                session, aircraft_id=body.aircraft_id, date_of_issue=issued_on,
                policy_id=body.policy_id, overrides=body.overrides())
            if draft.errors:
                raise _data_errors(draft)
            draft.data["issuer"], images = await issuer_mod.load(session, user)
        pdf = await run_in_threadpool(render, draft.data, reference_number=_preview_reference(draft),
                                      date_of_issue=issued_on, images=images, draft=True)
        return Response(content=pdf, media_type="application/pdf",
                        headers={"Content-Disposition": 'inline; filename="Reinsurance-Certificate-DRAFT.pdf"'})
    except RequestValidationError:
        raise
    except reinsurance.NotFound as _ex:
        return warning_response(request=request, response=response, msg=str(_ex),
                                status_code=status.HTTP_404_NOT_FOUND)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


# ==============================================================================================
# the issuing company
# ==============================================================================================

async def _settings_payload(session) -> dict:
    values = await issuer_mod.load_settings(session)
    rows = {r.key: r for r in (await session.execute(
        select(Asset).where(Asset.key.in_(issuer_mod.COMPANY_ASSETS)))).scalars().all()}
    return {**values,
            "logo": _image_meta(rows.get("logo"), "/certificates/settings/logo"),
            "stamp": _image_meta(rows.get("stamp"), "/certificates/settings/stamp")}


@router.get(path="/settings",
            description="The issuing company as the certificates print it: names, address line, "
                        "legal footer, colours, and the logo and stamp (metadata + URL).",
            responses=build_responses(include=_OK), dependencies=_READ)
async def get_settings(request: Request, response: Response):
    try:
        async with request.app.state.db_client.read_session(DB) as session:
            data = await _settings_payload(session)
        return success_response(request=request, response=response, data=data)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.patch(path="/settings",
              description="Change how the issuing company is printed. Only the fields sent change. "
                          "Certificates already issued keep what they printed.",
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


def _company_asset(kind: str) -> str:
    if kind not in issuer_mod.COMPANY_ASSETS:
        raise RequestValidationError([{"loc": ("path", "kind"), "type": "value_error",
                                       "msg": "kind must be logo or stamp"}])
    return kind


@router.put(path="/settings/{kind}",
            description="Upload the company `logo` (top right of every page) or `stamp` (beside the "
                        "signature): multipart field `file`, PNG, JPEG or SVG (drawn as vector), up to "
                        "2 MB. Replaces the current one.",
            responses=build_responses(include=_OK))
async def upload_company_image(request: Request, response: Response, kind: str,
                               file: UploadFile = File(...),
                               token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        meta = await _upload(request, _company_asset(kind), file, token)
        return success_response(request=request, response=response,
                                data={**meta, "url": f"/certificates/settings/{kind}"},
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
        served = await _serve(request, _company_asset(kind))
        return served or warning_response(request=request, response=response,
                                          msg=f"No {kind} is set", status_code=status.HTTP_404_NOT_FOUND)
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
        removed = await _remove(request, _company_asset(kind), token)
        if not removed:
            return warning_response(request=request, response=response, msg=f"No {kind} is set",
                                    status_code=status.HTTP_404_NOT_FOUND)
        return success_response(request=request, response=response, data={"kind": kind},
                                msg=f"{kind.capitalize()} removed")
    except RequestValidationError:
        raise
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


# ==============================================================================================
# the signatory — the calling portal user
# ==============================================================================================

async def _signatory_payload(session, user) -> dict:
    row = (await session.execute(
        select(Signatory).where(Signatory.portal_user_id == user.id))).scalar_one_or_none()
    signature = (await session.execute(
        select(Asset).where(Asset.key == issuer_mod.signature_key(user.id)))).scalar_one_or_none()
    return {
        "portal_user_id": user.id,
        "name": row.name if row else None,
        "email": row.email if row else None,
        "title": row.title if row else None,
        "phone": row.phone if row else None,
        "printed_as": {"name": (row.name if row and row.name else None) or user.name,
                       "email": (row.email if row and row.email else None) or user.email,
                       "title": row.title if row else None, "phone": row.phone if row else None},
        "signature": _image_meta(signature, "/certificates/signatory/signature"),
    }


@router.get(path="/signatory",
            description="What certificates the calling portal user issues print under the signature: "
                        "`printed_as` (their portal name and e-mail unless overridden, title, phone) and "
                        "the signature image. 422 without a portal user.",
            responses=build_responses(include=_OK), dependencies=_READ)
async def get_signatory(request: Request, response: Response):
    try:
        user = _need_user()
        async with request.app.state.db_client.read_session(DB) as session:
            data = await _signatory_payload(session, user)
        return success_response(request=request, response=response, data=data)
    except RequestValidationError:
        raise
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.patch(path="/signatory",
              description="Set the calling portal user's title and phone (and, if needed, a name or "
                          "e-mail other than the portal's). Only the fields sent change.",
              responses=build_responses(include=_OK))
async def update_signatory(request: Request, response: Response, body: SignatoryPatch,
                           token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        user = _need_user()
        fields = {k: ((v or "").strip() or None) for k, v in body.model_dump(exclude_unset=True).items()}
        async with request.app.state.db_client.session(DB) as session:
            await set_actor(session, token)
            row = (await session.execute(
                select(Signatory).where(Signatory.portal_user_id == user.id))).scalar_one_or_none()
            if row is None:
                row = Signatory(portal_user_id=user.id)
                session.add(row)
            for key, value in fields.items():
                setattr(row, key, value)
            await session.flush()
            data = await _signatory_payload(session, user)
        return success_response(request=request, response=response, data=data)
    except RequestValidationError:
        raise
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.put(path="/signatory/signature",
            description="Upload the calling portal user's signature: multipart field `file`, PNG "
                        "(transparent background is best), JPEG or SVG, up to 2 MB.",
            responses=build_responses(include=_OK))
async def upload_signature(request: Request, response: Response, file: UploadFile = File(...),
                           token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        user = _need_user()
        meta = await _upload(request, issuer_mod.signature_key(user.id), file, token)
        return success_response(request=request, response=response,
                                data={**meta, "url": "/certificates/signatory/signature"},
                                msg="Signature saved")
    except RequestValidationError:
        raise
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.get(path="/signatory/signature",
            description="The calling portal user's signature image.",
            responses=build_responses(include=_OK), dependencies=_READ)
async def get_signature(request: Request, response: Response):
    try:
        user = _need_user()
        served = await _serve(request, issuer_mod.signature_key(user.id))
        return served or warning_response(request=request, response=response,
                                          msg="No signature is set", status_code=status.HTTP_404_NOT_FOUND)
    except RequestValidationError:
        raise
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.delete(path="/signatory/signature",
               description="Remove the calling portal user's signature image.",
               responses=build_responses(include=_OK))
async def delete_signature(request: Request, response: Response,
                           token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        user = _need_user()
        if not await _remove(request, issuer_mod.signature_key(user.id), token):
            return warning_response(request=request, response=response, msg="No signature is set",
                                    status_code=status.HTTP_404_NOT_FOUND)
        return success_response(request=request, response=response, data={}, msg="Signature removed")
    except RequestValidationError:
        raise
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)
