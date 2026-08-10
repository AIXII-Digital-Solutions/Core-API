"""Aircraft insurance: add a record, update a record, read everything known about an aircraft.

The write body is a flat, 1:1 mirror of the source schedule row (registration / msn / airline /
aircraft_type / mtow / policy_from ... / source) — callers do not deal with surrogate ids. This
router normalises that row into the `api` schema:

    airline / aircraft_type / engines_type / lessee / lessor   ->  find-or-create reference rows
    registration + msn                                          ->  api.aircrafts (msn is identity)
    mtow + number_of_engines                                    ->  api.aircraft_specs (1:1, optional)
    engine_msn_1..4                                             ->  api.aircraft_engines (positions)
    policy_from/policy_to/CSL/currency                          ->  api.insurance_policies (shared)
    everything else                                             ->  api.insurance_records

History is never overwritten: a renewal is a new policy + a new record (the old one keeps its
period), and an in-place PATCH is captured pre-image-and-post-image by the DB audit trigger into
api.insurance_record_history. A 409 means the GiST exclusion constraint rejected an overlap — the
aircraft already has an insurance record covering part of that period.

Spelling note: the source columns are `depriciation_date` / `depriciation_rate`; the API and the
schema use the correct `depreciation_*`. Map on ingest, do not propagate the typo.
"""
from datetime import date
from decimal import Decimal
from typing import Optional, Any

from fastapi import Request, Response, Depends, Query, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select, or_, and_, func
from sqlalchemy.orm import aliased
from sqlalchemy.exc import IntegrityError

from Config import setup_logger
from settings import Router
from Database import ApiToken
from Database.ApiModels import (
    Airlines, Aircrafts, AircraftTypes, AircraftSpecs, AircraftEngines,
    InsurancePolicies, InsuranceRecords, InsuranceRecordHistory, InsuranceClaims,
    InsuranceStatus, InsuranceSource,
)
from api_auth import authorize, SCOPE_INSURANCE_READ, SCOPE_INSURANCE_WRITE
from Utils import success_response, warning_response, error_response
from Utils.ResponsesFunc import build_responses
from Utils.InsuranceCommon import (
    norm_reg, num, party_json, policy_json, enum_value, set_actor, find_aircraft,
    resolve_fk_labels, audit_entry, apply_sort, SortError,
    get_or_create_airline, get_or_create_party, get_or_create_aircraft_type,
    get_or_create_engine_type,
)
from .Claims import claim_json

logger = setup_logger("insurance_api")

router = Router(prefix="/insurance", tags=["Insurance"])

_DB = "aixii"
_MAX_ENGINES = 4  # the source schedule carries engine_msn_1..4

# Two different airlines can be attached to one record: the airline INSURED by the policy, and the
# aircraft's current operator. They agree for an insured record and only the operator exists for a
# `not_insured` one, so the grid reads the coalesce of the two — hence two aliases of one table.
_PolicyAirline = aliased(Airlines, name="policy_airline")
_OperatorAirline = aliased(Airlines, name="operator_airline")
_AIRLINE = func.coalesce(_PolicyAirline.airline_name, _OperatorAirline.airline_name)

# Whitelist of grid-sortable fields: public name -> SQL expression. Anything not in here is
# rejected with a 400 rather than interpolated into ORDER BY.
_RECORD_SORTS = {
    "registration": Aircrafts.registration,
    "msn": Aircrafts.msn,
    "airline": _AIRLINE,
    "aircraft_type": AircraftTypes.name,
    "mtow": AircraftSpecs.mtow_kg,
    "number_of_engines": AircraftSpecs.number_of_engines,
    "status": InsuranceRecords.status,
    "source": InsuranceRecords.source,
    "effective_from": InsuranceRecords.effective_from,
    "effective_to": InsuranceRecords.effective_to,
    "policy_from": InsurancePolicies.policy_from,
    "policy_to": InsurancePolicies.policy_to,
    "policy_number": InsurancePolicies.policy_number,
    "hull_deductible": InsuranceRecords.hull_deductible,
    "hull_spares_deductible": InsuranceRecords.hull_spares_deductible,
    "combined_single_limit": func.coalesce(InsuranceRecords.combined_single_limit,
                                           InsurancePolicies.combined_single_limit),
    "agreed_value_inception": InsuranceRecords.agreed_value_inception,
    "agreed_value": InsuranceRecords.agreed_value,
    "depreciation_date": InsuranceRecords.depreciation_date,
    "depreciation_rate": InsuranceRecords.depreciation_rate,
    "created_at": InsuranceRecords.created_at,
    "updated_at": InsuranceRecords.updated_at,
}


# ==============================================================================================
# Request bodies — field names deliberately match the source schedule's columns
# ==============================================================================================

class InsuranceCreate(BaseModel):
    # --- aircraft identity
    registration: str = Field(min_length=1, max_length=32)
    msn: Optional[str] = Field(default=None, max_length=64)
    airline: Optional[str] = Field(default=None, max_length=256)
    aircraft_type: Optional[str] = Field(default=None, max_length=128)

    # --- technical block (all optional; omit it entirely and no api.aircraft_specs row is created)
    mtow: Optional[Decimal] = Field(default=None, gt=0, description="Maximum take-off weight, KILOGRAMS.")
    number_of_engines: Optional[int] = Field(default=None, ge=1, le=8)
    engines_type: Optional[str] = Field(default=None, max_length=128)
    engine_msn_1: Optional[str] = Field(default=None, max_length=64)
    engine_msn_2: Optional[str] = Field(default=None, max_length=64)
    engine_msn_3: Optional[str] = Field(default=None, max_length=64)
    engine_msn_4: Optional[str] = Field(default=None, max_length=64)

    # --- policy (shared by every aircraft on the same contract)
    policy_from: date
    policy_to: Optional[date] = None
    policy_number: Optional[str] = Field(default=None, max_length=128)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    combined_single_limit: Optional[Decimal] = Field(default=None, ge=0)

    # --- this aircraft's record
    status: InsuranceStatus = InsuranceStatus.INSURED
    source: InsuranceSource
    lessee: Optional[str] = Field(default=None, max_length=256)
    lessor: Optional[str] = Field(default=None, max_length=256)
    hull_deductible: Optional[Decimal] = Field(default=None, ge=0)
    hull_spares_deductible: Optional[Decimal] = Field(default=None, ge=0)
    agreed_value_inception: Optional[Decimal] = Field(default=None, ge=0)
    agreed_value: Optional[Decimal] = Field(default=None, ge=0)
    agreed_value_fixed: bool = False
    depreciation_date: Optional[date] = None
    depreciation_rate: Optional[Decimal] = Field(
        default=None, ge=0, le=1, description="Fraction per annum (0.05 = 5 %/year), NOT a percent.",
    )

    # --- optional per-aircraft coverage window inside the policy (delivery / redelivery mid-term).
    # Defaults to the full policy period.
    effective_from: Optional[date] = None
    effective_to: Optional[date] = None

    @model_validator(mode="after")
    def _check(self):
        if self.status is InsuranceStatus.INSURED and self.airline is None:
            raise ValueError("`airline` is required for an insured record (it owns the policy)")
        if self.policy_to is not None and self.policy_to < self.policy_from:
            raise ValueError("`policy_to` must not be earlier than `policy_from`")
        return self


class InsurancePatch(BaseModel):
    """Partial update of ONE insurance record. Only the fields present in the body are touched;
    the DB trigger archives the pre-image regardless. Sending `effective_to: null` explicitly
    re-opens the record's period."""
    status: Optional[InsuranceStatus] = None
    source: Optional[InsuranceSource] = None
    lessee: Optional[str] = Field(default=None, max_length=256)
    lessor: Optional[str] = Field(default=None, max_length=256)
    hull_deductible: Optional[Decimal] = Field(default=None, ge=0)
    hull_spares_deductible: Optional[Decimal] = Field(default=None, ge=0)
    combined_single_limit: Optional[Decimal] = Field(default=None, ge=0)
    agreed_value_inception: Optional[Decimal] = Field(default=None, ge=0)
    agreed_value: Optional[Decimal] = Field(default=None, ge=0)
    agreed_value_fixed: Optional[bool] = None
    depreciation_date: Optional[date] = None
    depreciation_rate: Optional[Decimal] = Field(default=None, ge=0, le=1)
    effective_from: Optional[date] = None
    effective_to: Optional[date] = None


# ==============================================================================================
# write helpers
# ==============================================================================================
# The generic find-or-create helpers live in Utils/InsuranceCommon.py — Claims.py needs the same
# ones. What stays here is the part that is specific to the flat insurance schedule row.

async def _get_or_create_aircraft(session, body: InsuranceCreate,
                                  airline: Optional[Airlines]) -> Aircrafts:
    ac_type = await get_or_create_aircraft_type(session, body.aircraft_type)

    row = await find_aircraft(session, registration=body.registration, msn=body.msn)
    if row is None:
        row = Aircrafts(
            registration=body.registration.strip(),
            msn=body.msn.strip() if body.msn else None,
            aircraft_type_id=ac_type.id if ac_type else None,
            airline_id=airline.id if airline else None,
        )
        session.add(row)
        await session.flush()
        return row

    # known airframe: fill gaps and follow re-registrations / operator changes, never blank out
    if body.msn and not row.msn:
        row.msn = body.msn.strip()
    if body.registration and norm_reg(body.registration) != row.registration_normalized:
        row.registration = body.registration.strip()
    if ac_type:
        row.aircraft_type_id = ac_type.id
    if airline:
        row.airline_id = airline.id
    return row


async def _upsert_specs(session, aircraft: Aircrafts, body: InsuranceCreate) -> None:
    """Technical data is optional: with nothing to write we leave the 1:1 row absent, which is how
    'no technical data' stays distinguishable from 'known to be empty'."""
    if body.mtow is None and body.number_of_engines is None:
        return
    row = (await session.execute(
        select(AircraftSpecs).where(AircraftSpecs.aircraft_id == aircraft.id)
    )).scalar_one_or_none()
    if row is None:
        row = AircraftSpecs(aircraft_id=aircraft.id)
        session.add(row)
    if body.mtow is not None:
        row.mtow_kg = body.mtow
    if body.number_of_engines is not None:
        row.number_of_engines = body.number_of_engines
    row.source = body.source
    await session.flush()


async def _sync_engines(session, aircraft: Aircrafts, body: InsuranceCreate) -> None:
    """Reconcile the currently-fitted engines against engine_msn_1..4.

    A position whose MSN changed is not overwritten — the fitted row is closed
    (installed_to = today) and a new one opened, so the swap stays in the history. Positions absent
    from the body are left untouched (partial update)."""
    provided = {
        pos: getattr(body, f"engine_msn_{pos}")
        for pos in range(1, _MAX_ENGINES + 1)
        if getattr(body, f"engine_msn_{pos}")
    }
    engine_type = await get_or_create_engine_type(session, body.engines_type)
    if not provided and engine_type is None:
        return

    fitted = {
        e.position: e for e in (await session.execute(
            select(AircraftEngines).where(
                AircraftEngines.aircraft_id == aircraft.id,
                AircraftEngines.installed_to.is_(None),
            )
        )).scalars().all()
    }

    today = date.today()
    for pos, msn in provided.items():
        msn = msn.strip()
        current = fitted.get(pos)
        if current is None:
            session.add(AircraftEngines(
                aircraft_id=aircraft.id, position=pos, engine_msn=msn,
                engine_type_id=engine_type.id if engine_type else None,
                installed_from=body.effective_from or body.policy_from,
            ))
        elif current.engine_msn != msn:
            current.installed_to = today            # close the outgoing engine, keep its history
            await session.flush()                   # free the partial-unique slot before reusing it
            session.add(AircraftEngines(
                aircraft_id=aircraft.id, position=pos, engine_msn=msn,
                engine_type_id=engine_type.id if engine_type else None,
                installed_from=today,
            ))
        elif engine_type is not None and current.engine_type_id != engine_type.id:
            current.engine_type_id = engine_type.id

    # engines_type given without any MSN: stamp the type onto whatever is fitted
    if engine_type is not None and not provided:
        for engine in fitted.values():
            engine.engine_type_id = engine_type.id
    await session.flush()


async def _get_or_create_policy(session, body: InsuranceCreate,
                                airline: Optional[Airlines]) -> Optional[InsurancePolicies]:
    """A `not_insured` record has no contract behind it (and the CHECK constraint allows that)."""
    if body.status is not InsuranceStatus.INSURED or airline is None:
        return None
    row = (await session.execute(
        select(InsurancePolicies).where(
            InsurancePolicies.airline_id == airline.id,
            InsurancePolicies.policy_from == body.policy_from,
            InsurancePolicies.policy_to.is_(body.policy_to) if body.policy_to is None
            else InsurancePolicies.policy_to == body.policy_to,
        )
    )).scalar_one_or_none()
    if row is None:
        row = InsurancePolicies(
            airline_id=airline.id,
            policy_number=body.policy_number,
            policy_from=body.policy_from,
            policy_to=body.policy_to,
            currency=body.currency.upper(),
            combined_single_limit=body.combined_single_limit,
        )
        session.add(row)
        await session.flush()
        return row

    # existing contract: fill in details this row knows and the policy does not
    if body.policy_number and not row.policy_number:
        row.policy_number = body.policy_number
    if body.combined_single_limit is not None and row.combined_single_limit is None:
        row.combined_single_limit = body.combined_single_limit
    return row


# ==============================================================================================
# serialization
# ==============================================================================================

def _record(r: InsuranceRecords) -> dict:
    policy = policy_json(r.policy)
    return {
        "id": r.id,
        "status": enum_value(r.status),
        "source": enum_value(r.source),
        "effective_from": r.effective_from.isoformat(),
        "effective_to": r.effective_to.isoformat() if r.effective_to else None,
        "policy": policy,
        # the record-level limit is an OVERRIDE; fall back to the policy's
        "combined_single_limit": num(r.combined_single_limit) or (
            policy["combined_single_limit"] if policy else None
        ),
        "currency": policy["currency"] if policy else None,
        # one field for the grid's Airline column: the policy's insured airline, falling back to the
        # aircraft's operator (a `not_insured` record has no policy at all)
        "airline": (policy["airline"] if policy and policy["airline"] else
                    (r.aircraft.airline.airline_name
                     if r.aircraft is not None and r.aircraft.airline else None)),
        "lessee": party_json(r.lessee),
        "lessor": party_json(r.lessor),
        "hull_deductible": num(r.hull_deductible),
        "hull_spares_deductible": num(r.hull_spares_deductible),
        "agreed_value_inception": num(r.agreed_value_inception),
        "agreed_value": num(r.agreed_value),
        "agreed_value_fixed": r.agreed_value_fixed,
        "depreciation_date": r.depreciation_date.isoformat() if r.depreciation_date else None,
        "depreciation_rate": num(r.depreciation_rate),
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


def _aircraft(a: Aircrafts, *, all_engines: bool = True) -> dict:
    """`all_engines=False` keeps only the currently fitted set — the grid wants the aircraft's
    present state, the aircraft card wants the swap history too."""
    specs = a.specs
    engines = a.engines if all_engines else [e for e in a.engines if e.installed_to is None]
    fitted = {e.position: e for e in a.engines if e.installed_to is None}
    types = {e.engine_type.name for e in fitted.values() if e.engine_type}
    return {
        "id": a.id,
        "registration": a.registration,
        "msn": a.msn,
        "aircraft_type": a.aircraft_type.name if a.aircraft_type else None,
        "airline": a.airline.airline_name if a.airline else None,
        "specs": None if specs is None else {
            "mtow_kg": num(specs.mtow_kg),
            "number_of_engines": specs.number_of_engines,
            "source": enum_value(specs.source),
        },
        # flat mirror of the source schedule's engine columns, so a grid needs no reshaping.
        # `engines_type` is null when the fitted engines are not all the same model — read
        # `engines[]` in that case.
        "engines_type": types.pop() if len(types) == 1 else None,
        **{
            f"engine_msn_{pos}": fitted[pos].engine_msn if pos in fitted else None
            for pos in range(1, _MAX_ENGINES + 1)
        },
        "engines": [
            {
                "position": e.position,
                "engine_msn": e.engine_msn,
                "engine_type": e.engine_type.name if e.engine_type else None,
                "installed_from": e.installed_from.isoformat() if e.installed_from else None,
                "installed_to": e.installed_to.isoformat() if e.installed_to else None,
                "current": e.installed_to is None,
            }
            for e in engines
        ],
    }


def _covers(r: InsuranceRecords, on: date) -> bool:
    return r.effective_from <= on and (r.effective_to is None or r.effective_to >= on)


# ==============================================================================================
# Endpoints
# ==============================================================================================

@router.post(
    "",
    description=(
        "Add an insurance record for one aircraft. The body is the flat schedule row; reference "
        "rows (airline, aircraft type, engine type, lessee, lessor) and the policy itself are "
        "found-or-created. Returns 409 when the aircraft already has a record overlapping the "
        "requested period — patch that record instead of inserting a second one."
    ),
    responses=build_responses(include={
        status.HTTP_201_CREATED, status.HTTP_400_BAD_REQUEST, status.HTTP_409_CONFLICT,
        status.HTTP_500_INTERNAL_SERVER_ERROR,
    }),
)
async def create_insurance(
    body: InsuranceCreate,
    request: Request,
    response: Response,
    token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE)),
):
    effective_from = body.effective_from or body.policy_from
    effective_to = body.effective_to if body.effective_to is not None else body.policy_to
    if effective_to is not None and effective_to < effective_from:
        return warning_response(request=request, response=response,
                                msg="`effective_to` must not be earlier than `effective_from`",
                                status_code=status.HTTP_400_BAD_REQUEST)
    try:
        async with request.app.state.db_client.session(_DB) as session:
            await set_actor(session, token)

            airline = await get_or_create_airline(session, body.airline)
            aircraft = await _get_or_create_aircraft(session, body, airline)
            await _upsert_specs(session, aircraft, body)
            await _sync_engines(session, aircraft, body)

            policy = await _get_or_create_policy(session, body, airline)
            lessee = await get_or_create_party(session, body.lessee, role="lessee")
            lessor = await get_or_create_party(session, body.lessor, role="lessor")

            record = InsuranceRecords(
                aircraft_id=aircraft.id,
                policy_id=policy.id if policy else None,
                effective_from=effective_from,
                effective_to=effective_to,
                status=body.status,
                source=body.source,
                lessee_id=lessee.id if lessee else None,
                lessor_id=lessor.id if lessor else None,
                hull_deductible=body.hull_deductible,
                hull_spares_deductible=body.hull_spares_deductible,
                # store the CSL on the record only when it differs from the policy's
                combined_single_limit=(
                    body.combined_single_limit
                    if policy is not None and body.combined_single_limit is not None
                    and body.combined_single_limit != policy.combined_single_limit
                    else None
                ),
                agreed_value_inception=body.agreed_value_inception,
                agreed_value=body.agreed_value,
                agreed_value_fixed=body.agreed_value_fixed,
                depreciation_date=body.depreciation_date,
                depreciation_rate=body.depreciation_rate,
            )
            session.add(record)
            await session.flush()
            record_id, aircraft_id = record.id, aircraft.id

        return success_response(
            request=request, response=response,
            data={"record_id": record_id, "aircraft_id": aircraft_id,
                  "registration": body.registration, "msn": body.msn},
            msg="Insurance record created", status_code=status.HTTP_201_CREATED,
        )
    except IntegrityError as ex:
        return _integrity_response(request, response, ex, body.registration)
    except Exception as ex:
        logger.error(f"create_insurance failed: {ex}")
        return error_response(request=request, response=response, exc=ex)


@router.patch(
    "/{record_id}",
    description=(
        "Update one insurance record in place (endorsement / correction). Only the fields present "
        "in the body change; the previous version is archived automatically into "
        "api.insurance_record_history. To record a RENEWAL, POST a new record instead."
    ),
    responses=build_responses(include={
        status.HTTP_200_OK, status.HTTP_400_BAD_REQUEST, status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT, status.HTTP_500_INTERNAL_SERVER_ERROR,
    }),
)
async def update_insurance(
    record_id: int,
    body: InsurancePatch,
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

            record = (await session.execute(
                select(InsuranceRecords).where(InsuranceRecords.id == record_id)
            )).scalar_one_or_none()
            if record is None:
                return warning_response(request=request, response=response,
                                        msg=f"No insurance record with id {record_id}",
                                        status_code=status.HTTP_404_NOT_FOUND)

            # validate the RESULTING period before touching the row — returning mid-block would
            # otherwise leave the session to commit a half-applied change on the way out.
            new_from = body.effective_from if "effective_from" in fields_set else record.effective_from
            new_to = body.effective_to if "effective_to" in fields_set else record.effective_to
            if new_from is None:
                return warning_response(request=request, response=response,
                                        msg="`effective_from` cannot be null",
                                        status_code=status.HTTP_400_BAD_REQUEST)
            if new_to is not None and new_to < new_from:
                return warning_response(request=request, response=response,
                                        msg="`effective_to` must not be earlier than `effective_from`",
                                        status_code=status.HTTP_400_BAD_REQUEST)

            if "lessee" in fields_set:
                party = await get_or_create_party(session, body.lessee, role="lessee")
                record.lessee_id = party.id if party else None
            if "lessor" in fields_set:
                party = await get_or_create_party(session, body.lessor, role="lessor")
                record.lessor_id = party.id if party else None

            for field in (
                "status", "source", "hull_deductible", "hull_spares_deductible",
                "combined_single_limit", "agreed_value_inception", "agreed_value",
                "agreed_value_fixed", "depreciation_date", "depreciation_rate",
                "effective_from", "effective_to",
            ):
                if field in fields_set:
                    setattr(record, field, getattr(body, field))

            await session.flush()
            # relationships were selectin-loaded by the SELECT above; a changed lessee_id/lessor_id
            # would otherwise serialise the stale party.
            await session.refresh(record)
            data = _record(record)
        return success_response(request=request, response=response, data=data,
                                msg="Insurance record updated")
    except IntegrityError as ex:
        return _integrity_response(request, response, ex, f"record {record_id}")
    except Exception as ex:
        logger.error(f"update_insurance failed: {ex}")
        return error_response(request=request, response=response, exc=ex)


@router.get(
    "/aircraft/{registration}",
    description=(
        "Everything known about one aircraft: identity, type, operator, technical data, fitted and "
        "removed engines, the insurance record in force on `on_date` (default today), every claim "
        "ever raised against it and, unless `history=false`, every record ever held. Lookup is "
        "separator-insensitive ('YLLTD' finds 'YL-LTD'); pass `msn` instead when two airframes have "
        "shared a tail number."
    ),
    responses=build_responses(include={
        status.HTTP_200_OK, status.HTTP_404_NOT_FOUND, status.HTTP_500_INTERNAL_SERVER_ERROR,
    }),
    dependencies=[Depends(authorize(SCOPE_INSURANCE_READ))],
)
async def get_aircraft_insurance(
    registration: str,
    request: Request,
    response: Response,
    msn: Optional[str] = Query(None, description="Disambiguate by MSN — wins over the registration."),
    on_date: Optional[date] = Query(None, description="Date the `current` record must cover. Default: today."),
    history: bool = Query(True, description="Include every past/future record, newest first."),
):
    try:
        async with request.app.state.db_client.session(_DB) as session:
            aircraft = await find_aircraft(session, registration=registration, msn=msn)
            if aircraft is None:
                return warning_response(
                    request=request, response=response,
                    msg=f"No aircraft found for registration '{registration}'"
                        + (f" / msn '{msn}'" if msn else ""),
                    status_code=status.HTTP_404_NOT_FOUND,
                )
            records = (await session.execute(
                select(InsuranceRecords)
                .where(InsuranceRecords.aircraft_id == aircraft.id)
                .order_by(InsuranceRecords.effective_from.desc())
            )).scalars().all()

            claims = (await session.execute(
                select(InsuranceClaims)
                .where(InsuranceClaims.aircraft_id == aircraft.id)
                .order_by(InsuranceClaims.date_of_loss.desc())
            )).scalars().all()

            on = on_date or date.today()
            current = next((r for r in records if _covers(r, on)), None)
            data: dict[str, Any] = {
                "aircraft": _aircraft(aircraft),
                "as_of": on.isoformat(),
                "current": _record(current) if current is not None else None,
                # the aircraft is already the subject of this response — don't repeat it per claim
                "claims": [claim_json(c, with_aircraft=False) for c in claims],
            }
            if history:
                data["history"] = [_record(r) for r in records]
        return success_response(request=request, response=response, data=data)
    except Exception as ex:
        logger.error(f"get_aircraft_insurance failed: {ex}")
        return error_response(request=request, response=response, exc=ex)


@router.get(
    "/{record_id}/history",
    description=(
        "Audit trail of one insurance record — every in-place UPDATE/DELETE, newest first. Each "
        "entry carries a ready-to-render `changes` list (field, old, new — foreign keys already "
        "resolved to names) alongside the raw pre-image and post-image rows. Renewals are NOT here: "
        "they are separate records under GET /insurance/aircraft/{registration}."
    ),
    responses=build_responses(include={
        status.HTTP_200_OK, status.HTTP_500_INTERNAL_SERVER_ERROR,
    }),
    dependencies=[Depends(authorize(SCOPE_INSURANCE_READ))],
)
async def get_record_audit(
    record_id: int,
    request: Request,
    response: Response,
    limit: int = Query(50, ge=1, le=500),
):
    try:
        async with request.app.state.db_client.session(_DB) as session:
            rows = (await session.execute(
                select(InsuranceRecordHistory)
                .where(InsuranceRecordHistory.record_id == record_id)
                .order_by(InsuranceRecordHistory.changed_at.desc())
                .limit(limit)
            )).scalars().all()
            # resolve every referenced party/airline/policy id in ONE pass, not per snapshot
            labels = await resolve_fk_labels(
                session, [r.old_row for r in rows] + [r.new_row for r in rows],
            )
        data = [audit_entry(r, labels, id_field="record_id") for r in rows]
        return success_response(request=request, response=response, data=data)
    except Exception as ex:
        logger.error(f"get_record_audit failed: {ex}")
        return error_response(request=request, response=response, exc=ex)


@router.get(
    "",
    description=(
        "List insurance records for the grid. `data` is `{records, total}` — `total` counts the "
        "WHOLE filtered set so the client can page. Every row carries the full `aircraft` block "
        "(type, specs, currently fitted engines and the flat engine_msn_1..4 mirror), so a grid "
        "showing technical columns needs no follow-up request per row; the engine SWAP HISTORY is "
        "only in GET /insurance/aircraft/{registration}. `on_date` keeps only records in force on "
        "that date; `q` matches registration (separator-insensitive) or MSN. Sort with "
        "`sort=<field>&order=asc|desc` — an unknown field returns 400 listing the allowed ones."
    ),
    responses=build_responses(include={
        status.HTTP_200_OK, status.HTTP_400_BAD_REQUEST, status.HTTP_500_INTERNAL_SERVER_ERROR,
    }),
    dependencies=[Depends(authorize(SCOPE_INSURANCE_READ))],
)
async def list_insurance(
    request: Request,
    response: Response,
    q: Optional[str] = Query(None, description="Registration or MSN substring."),
    airline: Optional[str] = Query(None, description="Airline name substring."),
    record_status: Optional[InsuranceStatus] = Query(None, alias="status"),
    on_date: Optional[date] = Query(None, description="Keep only records in force on this date."),
    sort: Optional[str] = Query(None, description=f"One of: {', '.join(sorted(_RECORD_SORTS))}."),
    order: Optional[str] = Query("asc", description="asc | desc. NULLs always sort last."),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    try:
        conds = []
        if record_status is not None:
            conds.append(InsuranceRecords.status == record_status)
        if on_date is not None:
            conds.append(and_(
                InsuranceRecords.effective_from <= on_date,
                or_(InsuranceRecords.effective_to.is_(None),
                    InsuranceRecords.effective_to >= on_date),
            ))
        if q:
            conds.append(or_(
                Aircrafts.registration_normalized.like(f"%{norm_reg(q)}%"),
                Aircrafts.msn.ilike(f"%{q.strip()}%"),
            ))
        if airline:
            conds.append(_AIRLINE.ilike(f"%{airline.strip()}%"))

        # every join is 1:1 or many-to-one, so none of them multiplies rows
        base = (
            select(InsuranceRecords)
            .join(Aircrafts, Aircrafts.id == InsuranceRecords.aircraft_id)
            .outerjoin(InsurancePolicies, InsurancePolicies.id == InsuranceRecords.policy_id)
            .outerjoin(_PolicyAirline, _PolicyAirline.id == InsurancePolicies.airline_id)
            .outerjoin(_OperatorAirline, _OperatorAirline.id == Aircrafts.airline_id)
            .outerjoin(AircraftTypes, AircraftTypes.id == Aircrafts.aircraft_type_id)
            .outerjoin(AircraftSpecs, AircraftSpecs.aircraft_id == Aircrafts.id)
            .where(*conds)
        )
        stmt = apply_sort(
            base, sort=sort, order=order, sortmap=_RECORD_SORTS,
            tiebreak=(InsuranceRecords.effective_from.desc(), InsuranceRecords.id.desc()),
        ).limit(limit).offset(offset)

        async with request.app.state.db_client.session(_DB) as session:
            rows = (await session.execute(stmt)).scalars().all()
            total = (await session.execute(
                base.with_only_columns(func.count(InsuranceRecords.id)).order_by(None)
            )).scalar_one()
            records = [
                # the grid gets the aircraft's PRESENT state; removed engines stay in the card
                {**_record(r), "aircraft": _aircraft(r.aircraft, all_engines=False)}
                for r in rows
            ]
        return success_response(request=request, response=response,
                                data={"records": records, "total": total})
    except SortError as ex:
        return warning_response(request=request, response=response, msg=str(ex),
                                status_code=status.HTTP_400_BAD_REQUEST)
    except Exception as ex:
        logger.error(f"list_insurance failed: {ex}")
        return error_response(request=request, response=response, exc=ex)


def _integrity_response(request: Request, response: Response, ex: IntegrityError, subject: str):
    """Turn the schema's own guarantees into meaningful 4xx answers instead of a blanket 500."""
    detail = str(getattr(ex, "orig", ex))
    if "ex_insurance_records_no_overlap" in detail:
        return warning_response(
            request=request, response=response,
            msg=f"{subject} already has an insurance record overlapping that period — "
                f"update the existing record instead of adding a second one",
            status_code=status.HTTP_409_CONFLICT,
        )
    if "ck_insurance_records_insured_needs_policy" in detail:
        return warning_response(
            request=request, response=response,
            msg="An 'insured' record needs a policy: supply `airline` and `policy_from`",
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    if "uq_aircrafts_msn" in detail:
        return warning_response(
            request=request, response=response,
            msg="That MSN is already registered to a different aircraft",
            status_code=status.HTTP_409_CONFLICT,
        )
    logger.error(f"insurance integrity error: {detail}")
    return error_response(request=request, response=response, exc=ex,
                          status_code=status.HTTP_409_CONFLICT,
                          msg="The change conflicts with existing data")
