"""Insurance claims: register a loss event, correct it, read it back with its full change history.

Like Routers/Insurance.py the write body is a flat 1:1 mirror of the source schedule row
(registration / msn / airline / policy_period / type_of_damage / date_of_loss / ... / hsl_paid) —
callers never deal with surrogate ids. This router resolves it into the `api` schema:

    registration + msn        ->  api.aircrafts        (find-or-create, MSN is identity)
    airline                   ->  api.airlines         (find-or-create)
    surveyor / leader         ->  api.parties          (find-or-create, role flags accumulate)
    policy_period             ->  api.insurance_policies — either the period given explicitly, or,
                                  when omitted, the policy that was in force for THAT aircraft on
                                  date_of_loss, taken from api.insurance_records
    everything else           ->  api.insurance_claims

The change history is the point of the table, not a side effect: `api.insurance_claims_audit()`
snapshots every INSERT / UPDATE / DELETE into api.insurance_claim_history, and
GET /insurance/claims/{id}/history turns those snapshots into a `changes` list (field, old, new —
foreign keys already resolved to names) that a UI can render straight into an info button.

Claims are NOT mutually exclusive: one aircraft can suffer several losses on the same day, so there
is no overlap constraint here (unlike api.insurance_records).
"""
from datetime import date
from decimal import Decimal
from typing import Optional, Any

from fastapi import Request, Response, Depends, Query, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select, or_, func
from sqlalchemy.exc import IntegrityError

from Config import setup_logger
from settings import Router
from Database import ApiToken
from Database.ApiModels import (
    Aircrafts, Airlines, InsurancePolicies, InsuranceRecords,
    InsuranceClaims, InsuranceClaimHistory, ClaimDamageType,
)
from api_auth import authorize, SCOPE_INSURANCE_READ, SCOPE_INSURANCE_WRITE
from Utils import success_response, warning_response, error_response
from Utils.ResponsesFunc import build_responses
from Utils.InsuranceCommon import (
    norm_reg, num, party_json, policy_json, enum_value, set_actor, find_aircraft,
    get_or_create_airline, get_or_create_party, resolve_fk_labels, audit_entry,
)

logger = setup_logger("insurance_claims_api")

router = Router(prefix="/insurance/claims", tags=["Insurance"])

_DB = "aixii"

# the per-section money columns, in the order the schedule lists them
_AMOUNT_FIELDS = (
    "indemnity_reserve", "paid_amount",
    "hd_reserve", "hd_paid", "hw_reserve", "hw_paid", "hsl_reserve", "hsl_paid",
)


# ==============================================================================================
# Request bodies — field names deliberately match the source schedule's columns
# ==============================================================================================

class ClaimCreate(BaseModel):
    # --- what was damaged
    registration: str = Field(min_length=1, max_length=32)
    msn: Optional[str] = Field(default=None, max_length=64)
    airline: Optional[str] = Field(default=None, max_length=256)

    # --- `policy_period`. Give the period explicitly, or omit it and let the claim attach to
    # whatever policy covered this aircraft on date_of_loss.
    policy_from: Optional[date] = None
    policy_to: Optional[date] = None
    policy_number: Optional[str] = Field(default=None, max_length=128)

    # --- the loss
    type_of_damage: ClaimDamageType
    date_of_loss: date
    location_of_loss: Optional[str] = Field(default=None, max_length=512)
    damage: Optional[str] = Field(default=None, description="Free-text description of the damage.")
    claim_reference: Optional[str] = Field(
        default=None, max_length=128, description="The insurer's claim number, if there is one. Unique.",
    )

    # --- who is handling it
    surveyor: Optional[str] = Field(default=None, max_length=256)
    leader: Optional[str] = Field(default=None, max_length=256,
                                  description="Lead underwriter on the claim.")

    # --- money
    currency: str = Field(default="USD", min_length=3, max_length=3)
    indemnity_reserve: Optional[Decimal] = Field(default=None, ge=0)
    paid_amount: Optional[Decimal] = Field(default=None, ge=0)
    paid_date: Optional[date] = None
    # per-section breakdown: HD = hull deductible, HW = hull war, HSL = hull & spares
    hd_reserve: Optional[Decimal] = Field(default=None, ge=0)
    hd_paid: Optional[Decimal] = Field(default=None, ge=0)
    hw_reserve: Optional[Decimal] = Field(default=None, ge=0)
    hw_paid: Optional[Decimal] = Field(default=None, ge=0)
    hsl_reserve: Optional[Decimal] = Field(default=None, ge=0)
    hsl_paid: Optional[Decimal] = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _check(self):
        if self.paid_date is not None and self.paid_date < self.date_of_loss:
            raise ValueError("`paid_date` must not be earlier than `date_of_loss`")
        if self.policy_to is not None and self.policy_from is not None \
                and self.policy_to < self.policy_from:
            raise ValueError("`policy_to` must not be earlier than `policy_from`")
        return self


class ClaimPatch(BaseModel):
    """Partial update of ONE claim — a reserve movement, a settlement, a correction. Only the fields
    present in the body are touched, and the trigger archives the pre-image regardless.

    The aircraft is NOT patchable: a claim raised against the wrong airframe is a different claim,
    not a corrected one. Everything else, including the policy period, is."""
    airline: Optional[str] = Field(default=None, max_length=256)
    policy_from: Optional[date] = None
    policy_to: Optional[date] = None
    policy_number: Optional[str] = Field(default=None, max_length=128)

    type_of_damage: Optional[ClaimDamageType] = None
    date_of_loss: Optional[date] = None
    location_of_loss: Optional[str] = Field(default=None, max_length=512)
    damage: Optional[str] = None
    claim_reference: Optional[str] = Field(default=None, max_length=128)

    surveyor: Optional[str] = Field(default=None, max_length=256)
    leader: Optional[str] = Field(default=None, max_length=256)

    currency: Optional[str] = Field(default=None, min_length=3, max_length=3)
    indemnity_reserve: Optional[Decimal] = Field(default=None, ge=0)
    paid_amount: Optional[Decimal] = Field(default=None, ge=0)
    paid_date: Optional[date] = None
    hd_reserve: Optional[Decimal] = Field(default=None, ge=0)
    hd_paid: Optional[Decimal] = Field(default=None, ge=0)
    hw_reserve: Optional[Decimal] = Field(default=None, ge=0)
    hw_paid: Optional[Decimal] = Field(default=None, ge=0)
    hsl_reserve: Optional[Decimal] = Field(default=None, ge=0)
    hsl_paid: Optional[Decimal] = Field(default=None, ge=0)


# ==============================================================================================
# resolution helpers
# ==============================================================================================

async def _get_or_create_aircraft(session, registration: str, msn: Optional[str],
                                  airline: Optional[Airlines]) -> Aircrafts:
    """A claim can legitimately arrive before the aircraft's insurance schedule does, so an unknown
    airframe is created rather than rejected — with identity only, no invented technical data."""
    row = await find_aircraft(session, registration=registration, msn=msn)
    if row is None:
        row = Aircrafts(
            registration=registration.strip(),
            msn=msn.strip() if msn else None,
            airline_id=airline.id if airline else None,
        )
        session.add(row)
        await session.flush()
        return row
    if msn and not row.msn:
        row.msn = msn.strip()
    return row


async def _resolve_policy(session, *, airline: Optional[Airlines], aircraft: Aircrafts,
                          policy_from: Optional[date], policy_to: Optional[date],
                          policy_number: Optional[str],
                          on_date: date) -> Optional[InsurancePolicies]:
    """Turn the source's `policy_period` into a real contract row.

    With an explicit period this is the same find-or-create the insurance schedule uses, so a claim
    can be loaded before its policy. With no period it falls back to the policy that actually
    covered THIS aircraft on the loss date — which is what `policy_period` means in the source, and
    saves the caller retyping a period the database already knows.
    """
    if policy_from is not None:
        if airline is None:
            return None       # cannot key a policy without its insured airline
        row = (await session.execute(
            select(InsurancePolicies).where(
                InsurancePolicies.airline_id == airline.id,
                InsurancePolicies.policy_from == policy_from,
                InsurancePolicies.policy_to.is_(None) if policy_to is None
                else InsurancePolicies.policy_to == policy_to,
            )
        )).scalar_one_or_none()
        if row is None:
            row = InsurancePolicies(
                airline_id=airline.id, policy_number=policy_number,
                policy_from=policy_from, policy_to=policy_to,
            )
            session.add(row)
            await session.flush()
        elif policy_number and not row.policy_number:
            row.policy_number = policy_number
        return row

    record = (await session.execute(
        select(InsuranceRecords).where(
            InsuranceRecords.aircraft_id == aircraft.id,
            InsuranceRecords.policy_id.is_not(None),
            InsuranceRecords.effective_from <= on_date,
            or_(InsuranceRecords.effective_to.is_(None),
                InsuranceRecords.effective_to >= on_date),
        ).order_by(InsuranceRecords.effective_from.desc()).limit(1)
    )).scalar_one_or_none()
    return record.policy if record is not None else None


# ==============================================================================================
# serialization
# ==============================================================================================

def claim_json(c: InsuranceClaims, *, with_aircraft: bool = True) -> dict:
    reserve, paid = c.indemnity_reserve, c.paid_amount
    data: dict[str, Any] = {
        "id": c.id,
        "claim_reference": c.claim_reference,
        "type_of_damage": enum_value(c.type_of_damage),
        "date_of_loss": c.date_of_loss.isoformat(),
        "location_of_loss": c.location_of_loss,
        "damage": c.damage,
        "airline": c.airline.airline_name if c.airline else None,
        "policy": policy_json(c.policy),
        "surveyor": party_json(c.surveyor),
        "leader": party_json(c.leader),
        "currency": c.currency,
        "indemnity_reserve": num(reserve),
        "paid_amount": num(paid),
        "paid_date": c.paid_date.isoformat() if c.paid_date else None,
        "hd_reserve": num(c.hd_reserve),
        "hd_paid": num(c.hd_paid),
        "hw_reserve": num(c.hw_reserve),
        "hw_paid": num(c.hw_paid),
        "hsl_reserve": num(c.hsl_reserve),
        "hsl_paid": num(c.hsl_paid),
        # derived, never stored: the reserve still standing, and whether anything has been settled
        "outstanding": None if reserve is None else num(reserve - (paid or 0)),
        "is_settled": c.paid_date is not None,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }
    if with_aircraft:
        data["aircraft"] = {
            "id": c.aircraft.id, "registration": c.aircraft.registration, "msn": c.aircraft.msn,
        }
    return data


# ==============================================================================================
# Endpoints
# ==============================================================================================

@router.post(
    "",
    description=(
        "Register a loss event. The body is the flat schedule row; the aircraft, airline, surveyor "
        "and leader are found-or-created, and the policy is resolved either from the period given "
        "or from whichever policy covered this aircraft on `date_of_loss`. The creation itself is "
        "recorded in the claim's history, so the timeline starts here."
    ),
    responses=build_responses(include={
        status.HTTP_201_CREATED, status.HTTP_400_BAD_REQUEST, status.HTTP_409_CONFLICT,
        status.HTTP_500_INTERNAL_SERVER_ERROR,
    }),
)
async def create_claim(
    body: ClaimCreate,
    request: Request,
    response: Response,
    token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE)),
):
    try:
        async with request.app.state.db_client.session(_DB) as session:
            await set_actor(session, token)

            airline = await get_or_create_airline(session, body.airline)
            aircraft = await _get_or_create_aircraft(session, body.registration, body.msn, airline)
            if airline is None and aircraft.airline_id is not None:
                airline = await session.get(Airlines, aircraft.airline_id)

            policy = await _resolve_policy(
                session, airline=airline, aircraft=aircraft,
                policy_from=body.policy_from, policy_to=body.policy_to,
                policy_number=body.policy_number, on_date=body.date_of_loss,
            )
            # the claim's own airline falls back to the policy's insured, then to the operator
            if airline is None and policy is not None:
                airline = await session.get(Airlines, policy.airline_id)

            surveyor = await get_or_create_party(session, body.surveyor, role="surveyor")
            leader = await get_or_create_party(session, body.leader, role="leader")

            claim = InsuranceClaims(
                aircraft_id=aircraft.id,
                airline_id=airline.id if airline else None,
                policy_id=policy.id if policy else None,
                claim_reference=body.claim_reference,
                type_of_damage=body.type_of_damage,
                date_of_loss=body.date_of_loss,
                location_of_loss=body.location_of_loss,
                damage=body.damage,
                surveyor_id=surveyor.id if surveyor else None,
                leader_id=leader.id if leader else None,
                currency=body.currency.upper(),
                paid_date=body.paid_date,
                **{f: getattr(body, f) for f in _AMOUNT_FIELDS},
            )
            session.add(claim)
            await session.flush()
            claim_id, aircraft_id = claim.id, aircraft.id
            policy_id = policy.id if policy else None

        return success_response(
            request=request, response=response,
            data={"claim_id": claim_id, "aircraft_id": aircraft_id, "policy_id": policy_id,
                  "registration": body.registration, "msn": body.msn},
            msg="Claim created", status_code=status.HTTP_201_CREATED,
        )
    except IntegrityError as ex:
        return _integrity_response(request, response, ex)
    except Exception as ex:
        logger.error(f"create_claim failed: {ex}")
        return error_response(request=request, response=response, exc=ex)


@router.patch(
    "/{claim_id}",
    description=(
        "Update one claim — a reserve movement, a settlement, a correction. Only the fields present "
        "in the body change, and the previous version is archived automatically, so the UI can "
        "always show what the values were before. The aircraft cannot be changed."
    ),
    responses=build_responses(include={
        status.HTTP_200_OK, status.HTTP_400_BAD_REQUEST, status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT, status.HTTP_500_INTERNAL_SERVER_ERROR,
    }),
)
async def update_claim(
    claim_id: int,
    body: ClaimPatch,
    request: Request,
    response: Response,
    token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE)),
):
    fields_set = body.model_fields_set
    if not fields_set:
        return warning_response(request=request, response=response,
                                msg="Empty body — nothing to update",
                                status_code=status.HTTP_400_BAD_REQUEST)
    try:
        async with request.app.state.db_client.session(_DB) as session:
            await set_actor(session, token)

            claim = (await session.execute(
                select(InsuranceClaims).where(InsuranceClaims.id == claim_id)
            )).scalar_one_or_none()
            if claim is None:
                return warning_response(request=request, response=response,
                                        msg=f"No claim with id {claim_id}",
                                        status_code=status.HTTP_404_NOT_FOUND)

            # validate the RESULTING dates before touching the row — returning mid-block would
            # otherwise leave the session to commit a half-applied change on the way out.
            new_loss = body.date_of_loss if "date_of_loss" in fields_set else claim.date_of_loss
            new_paid = body.paid_date if "paid_date" in fields_set else claim.paid_date
            if new_loss is None:
                return warning_response(request=request, response=response,
                                        msg="`date_of_loss` cannot be null",
                                        status_code=status.HTTP_400_BAD_REQUEST)
            if new_paid is not None and new_paid < new_loss:
                return warning_response(request=request, response=response,
                                        msg="`paid_date` must not be earlier than `date_of_loss`",
                                        status_code=status.HTTP_400_BAD_REQUEST)

            if "airline" in fields_set:
                airline = await get_or_create_airline(session, body.airline)
                claim.airline_id = airline.id if airline else None
            if "surveyor" in fields_set:
                party = await get_or_create_party(session, body.surveyor, role="surveyor")
                claim.surveyor_id = party.id if party else None
            if "leader" in fields_set:
                party = await get_or_create_party(session, body.leader, role="leader")
                claim.leader_id = party.id if party else None

            # re-point the claim at a different contract ("policy_period" was wrong)
            if "policy_from" in fields_set or "policy_to" in fields_set:
                airline = await session.get(Airlines, claim.airline_id) if claim.airline_id else None
                aircraft = await session.get(Aircrafts, claim.aircraft_id)
                policy = await _resolve_policy(
                    session, airline=airline, aircraft=aircraft,
                    policy_from=body.policy_from if "policy_from" in fields_set else None,
                    policy_to=body.policy_to,
                    policy_number=body.policy_number, on_date=new_loss,
                )
                claim.policy_id = policy.id if policy else None

            if "currency" in fields_set and body.currency:
                claim.currency = body.currency.upper()

            for field in (
                "type_of_damage", "date_of_loss", "location_of_loss", "damage", "claim_reference",
                "paid_date", *_AMOUNT_FIELDS,
            ):
                if field in fields_set:
                    setattr(claim, field, getattr(body, field))

            await session.flush()
            # relationships were selectin-loaded by the SELECT above; a changed surveyor_id /
            # leader_id / policy_id would otherwise serialise the stale row.
            await session.refresh(claim)
            data = claim_json(claim)
        return success_response(request=request, response=response, data=data,
                                msg="Claim updated")
    except IntegrityError as ex:
        return _integrity_response(request, response, ex)
    except Exception as ex:
        logger.error(f"update_claim failed: {ex}")
        return error_response(request=request, response=response, exc=ex)


@router.get(
    "/{claim_id}/history",
    description=(
        "Change history of one claim, newest first — this is what the frontend's info button reads. "
        "Each entry carries `changes`: a list of {field, old, new} with foreign keys already "
        "resolved to names, so 'surveyor: Charles Taylor -> McLarens' needs no further lookups. The "
        "raw `old_row` / `new_row` snapshots come along for anything the diff skips. The first "
        "entry (operation INSERT) is the claim being opened."
    ),
    responses=build_responses(include={
        status.HTTP_200_OK, status.HTTP_500_INTERNAL_SERVER_ERROR,
    }),
    dependencies=[Depends(authorize(SCOPE_INSURANCE_READ))],
)
async def get_claim_history(
    claim_id: int,
    request: Request,
    response: Response,
    limit: int = Query(100, ge=1, le=500),
):
    try:
        async with request.app.state.db_client.session(_DB) as session:
            rows = (await session.execute(
                select(InsuranceClaimHistory)
                .where(InsuranceClaimHistory.claim_id == claim_id)
                .order_by(InsuranceClaimHistory.changed_at.desc(),
                          InsuranceClaimHistory.id.desc())
                .limit(limit)
            )).scalars().all()
            # resolve every referenced party/airline/policy id in ONE pass, not per snapshot
            labels = await resolve_fk_labels(
                session, [r.old_row for r in rows] + [r.new_row for r in rows],
            )
        data = [audit_entry(r, labels, id_field="claim_id") for r in rows]
        return success_response(request=request, response=response, data=data)
    except Exception as ex:
        logger.error(f"get_claim_history failed: {ex}")
        return error_response(request=request, response=response, exc=ex)


@router.get(
    "/{claim_id}",
    description="One claim in full, with its aircraft, policy, surveyor and leader resolved.",
    responses=build_responses(include={
        status.HTTP_200_OK, status.HTTP_404_NOT_FOUND, status.HTTP_500_INTERNAL_SERVER_ERROR,
    }),
    dependencies=[Depends(authorize(SCOPE_INSURANCE_READ))],
)
async def get_claim(claim_id: int, request: Request, response: Response):
    try:
        async with request.app.state.db_client.session(_DB) as session:
            claim = (await session.execute(
                select(InsuranceClaims).where(InsuranceClaims.id == claim_id)
            )).scalar_one_or_none()
            if claim is None:
                return warning_response(request=request, response=response,
                                        msg=f"No claim with id {claim_id}",
                                        status_code=status.HTTP_404_NOT_FOUND)
            data = claim_json(claim)
        return success_response(request=request, response=response, data=data)
    except Exception as ex:
        logger.error(f"get_claim failed: {ex}")
        return error_response(request=request, response=response, exc=ex)


@router.get(
    "",
    description=(
        "List claims, newest loss first. `q` matches registration (separator-insensitive), MSN or "
        "claim reference; `settled` splits paid claims from open ones. `data` is "
        "`{claims, totals}` — the totals (count, reserve, paid, outstanding) are summed over the "
        "WHOLE filtered set, not just the returned page, so paging never distorts the exposure."
    ),
    responses=build_responses(include={
        status.HTTP_200_OK, status.HTTP_500_INTERNAL_SERVER_ERROR,
    }),
    dependencies=[Depends(authorize(SCOPE_INSURANCE_READ))],
)
async def list_claims(
    request: Request,
    response: Response,
    q: Optional[str] = Query(None, description="Registration, MSN or claim reference substring."),
    airline: Optional[str] = Query(None, description="Airline name substring."),
    type_of_damage: Optional[ClaimDamageType] = Query(None),
    date_from: Optional[date] = Query(None, description="Earliest date_of_loss to include."),
    date_to: Optional[date] = Query(None, description="Latest date_of_loss to include."),
    settled: Optional[bool] = Query(None, description="true = paid_date set, false = still open."),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    try:
        conds = []
        if type_of_damage is not None:
            conds.append(InsuranceClaims.type_of_damage == type_of_damage)
        if date_from is not None:
            conds.append(InsuranceClaims.date_of_loss >= date_from)
        if date_to is not None:
            conds.append(InsuranceClaims.date_of_loss <= date_to)
        if settled is not None:
            conds.append(InsuranceClaims.paid_date.is_not(None) if settled
                         else InsuranceClaims.paid_date.is_(None))
        if q:
            conds.append(or_(
                Aircrafts.registration_normalized.like(f"%{norm_reg(q)}%"),
                Aircrafts.msn.ilike(f"%{q.strip()}%"),
                InsuranceClaims.claim_reference.ilike(f"%{q.strip()}%"),
            ))
        if airline:
            conds.append(Airlines.airline_name.ilike(f"%{airline.strip()}%"))

        base = (
            select(InsuranceClaims)
            .join(Aircrafts, Aircrafts.id == InsuranceClaims.aircraft_id)
            .outerjoin(Airlines, Airlines.id == InsuranceClaims.airline_id)
            .where(*conds)
        )
        async with request.app.state.db_client.session(_DB) as session:
            rows = (await session.execute(
                base.order_by(InsuranceClaims.date_of_loss.desc(), InsuranceClaims.id.desc())
                    .limit(limit).offset(offset)
            )).scalars().all()
            # totals over the whole filtered set — a page of claims tells you nothing about exposure
            totals = (await session.execute(
                base.with_only_columns(
                    func.count(InsuranceClaims.id),
                    func.coalesce(func.sum(InsuranceClaims.indemnity_reserve), 0),
                    func.coalesce(func.sum(InsuranceClaims.paid_amount), 0),
                ).order_by(None)
            )).one()
            claims = [claim_json(c) for c in rows]
        count, reserve, paid = totals
        data = {
            "claims": claims,
            "totals": {
                "count": count,
                "indemnity_reserve": float(reserve),
                "paid_amount": float(paid),
                "outstanding": float(reserve - paid),
            },
        }
        return success_response(request=request, response=response, data=data)
    except Exception as ex:
        logger.error(f"list_claims failed: {ex}")
        return error_response(request=request, response=response, exc=ex)


def _integrity_response(request: Request, response: Response, ex: IntegrityError):
    """Turn the schema's own guarantees into meaningful 4xx answers instead of a blanket 500."""
    detail = str(getattr(ex, "orig", ex))
    if "uq_insurance_claims_reference" in detail:
        return warning_response(
            request=request, response=response,
            msg="That claim reference is already used by another claim",
            status_code=status.HTTP_409_CONFLICT,
        )
    if "ck_insurance_claims_paid_date" in detail:
        return warning_response(
            request=request, response=response,
            msg="`paid_date` must not be earlier than `date_of_loss`",
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    if "ck_insurance_claims_amounts_non_negative" in detail:
        return warning_response(
            request=request, response=response,
            msg="Claim amounts must not be negative",
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    if "uq_aircrafts_msn" in detail:
        return warning_response(
            request=request, response=response,
            msg="That MSN is already registered to a different aircraft",
            status_code=status.HTTP_409_CONFLICT,
        )
    logger.error(f"claims integrity error: {detail}")
    return error_response(request=request, response=response, exc=ex,
                          status_code=status.HTTP_409_CONFLICT,
                          msg="The change conflicts with existing data")
