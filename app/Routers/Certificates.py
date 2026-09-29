"""Certificates for insured aircraft: `/certificates/reinsurance` (AVN 67B).

    POST /certificates/reinsurance/preview   what the certificate would say — values, alerts, errors —
                                             without numbering or saving anything
    POST /certificates/reinsurance           issue it: number it, draw the PDF, keep both in history
    GET  /certificates/reinsurance           the history, newest first
    GET  /certificates/reinsurance/{id}      one issued certificate with every value it printed
    GET  /certificates/reinsurance/{id}/pdf  the PDF exactly as issued

A certificate is issued for an INSURED aircraft only — the policy is the one named, else the one
covering it on the date of issue, else the next one to start. The values come from the policy, the
aircraft and the lease (Certificates/reinsurance.py says which from where); what an e-mail, rider or
mark-up says instead is sent with the request and kept with the certificate.

The history is append-only: a correction is a new certificate with a new number, never an edit.
"""
from datetime import date
from decimal import Decimal
from typing import Optional

from fastapi import Depends, Query, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from Certificates import numbering, reinsurance
from Certificates.render import render
from Config import setup_logger
from Database import ApiToken
from Database.CertificateModels import ReinsuranceCertificate
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
        async with request.app.state.db_client.read_session(DB) as session:
            draft = await reinsurance.assemble(
                session, aircraft_id=body.aircraft_id, date_of_issue=issued_on,
                policy_id=body.policy_id, overrides=body.overrides())
        return success_response(request=request, response=response, data={
            "reference_number": _preview_reference(draft),
            "date_of_issue": issued_on.isoformat(),
            "variant": draft.variant,
            "data": draft.data,
            "alerts": draft.alerts,
            "errors": [{"field": f"certificate.{e['field']}", "msg": e["msg"]} for e in draft.errors],
            "can_issue": not draft.errors,
        })
    except reinsurance.NotFound as _ex:
        return warning_response(request=request, response=response, msg=str(_ex),
                                status_code=status.HTTP_404_NOT_FOUND)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.post(
    path="/reinsurance",
    description=(
        "Issue the reinsurance certificate: resolve every value (as the preview does), refuse with "
        "422 if any the document needs is missing (`field` = `certificate.<name>`), allocate the "
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
        async with request.app.state.db_client.session(DB) as session:
            await set_actor(session, token)
            draft = await reinsurance.assemble(
                session, aircraft_id=body.aircraft_id, date_of_issue=issued_on,
                policy_id=body.policy_id, overrides=body.overrides())
            if draft.errors:
                raise _data_errors(draft)
            scope, sequence = await numbering.next_sequence(session, reinsurance.KIND)
            reference = numbering.reference_number(draft.contract_year, draft.airline_code, sequence)
            # CPU work, a few tens of milliseconds: off the event loop
            pdf = await run_in_threadpool(render, draft.data, reference_number=reference,
                                          date_of_issue=issued_on)
            row = ReinsuranceCertificate(
                reference_number=reference, contract_year=draft.contract_year,
                airline_code=draft.airline_code, sequence_no=sequence, counter_scope=scope,
                date_of_issue=issued_on,
                date_of_issue_source="user" if body.date_of_issue else "system",
                variant=draft.variant, template_version=reinsurance.TEMPLATE_VERSION,
                registration=draft.registration, msn=draft.msn,
                data=draft.data, alerts=draft.alerts, pdf=pdf,
                issued_by=token.name if token is not None else "service-token",
                issued_by_user_id=user.id if user and token is None else None,
                issued_by_user_email=user.email if user and token is None else None,
                issued_by_user_name=user.name if user and token is None else None,
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
