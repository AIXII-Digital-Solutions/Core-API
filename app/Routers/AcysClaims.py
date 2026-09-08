"""Airline claims experience (forecast.acys_claims) — the aggregated claims sheet, per airline/year.

One row is "this airline had N claims worth X in this calendar year, under this section of cover, in
this state". It is loaded as stated (a broker's claims-experience summary), NOT derived from
individual loss events.

NOT to be confused with /insurance/claims (api.insurance_claims), which registers ONE loss against a
specific aircraft and policy, with a full change history. This router is the summary table that feeds
reporting; that one is the operational record.

The grain — and the natural key a re-import upserts on — is
    airline x calendar_year x currency x policy_type x claims_status
so Settled and Ongoing are separate rows for the same year, as are two currencies. Amounts are never
summed across currencies: nothing here converts them.

    GET    /forecast/claims           list, filtered (airline, year range, currency, type, status)
    GET    /forecast/claims/{id}      one row
    POST   /forecast/claims           add one row
    POST   /forecast/claims/bulk      load many rows; `upsert=true` overwrites rows of the same grain
    PATCH  /forecast/claims/{id}      correct a row
    DELETE /forecast/claims/{id}      remove a row

PowerBI reads the table (or a service-side filtered slice of it) directly as `bi_reader`; this router
is the write path and the ad-hoc read path.
"""
from decimal import Decimal
from typing import Optional, List

from fastapi import Request, Response, Depends, Query, Path, status
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select, func, delete as sa_delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from Config import setup_logger
from settings import Router
from Database.ForecastModels import (
    AcysClaims, ClaimCurrency, ClaimPolicyType, ClaimsStatus,
)
from api_auth import authorize, SCOPE_PREDICTIVE_READ, SCOPE_PREDICTIVE_WRITE
from Utils import success_response, warning_response, error_response
from Utils.ResponsesFunc import build_responses

logger = setup_logger("acys_claims_api")

router = Router(prefix="/forecast/claims", tags=["Forecast"])

_DB = "aixii"

# the columns that make a row unique — the upsert target and what a duplicate POST collides on
_GRAIN = ("airline", "calendar_year", "currency", "policy_type", "claims_status")

_SORTABLE = {
    "airline": AcysClaims.airline,
    "calendar_year": AcysClaims.calendar_year,
    "number_of_claims": AcysClaims.number_of_claims,
    "claim_amount": AcysClaims.claim_amount,
    "currency": AcysClaims.currency,
    "policy_type": AcysClaims.policy_type,
    "claims_status": AcysClaims.claims_status,
    "created_at": AcysClaims.created_at,
    "updated_at": AcysClaims.updated_at,
}


class ClaimRow(BaseModel):
    """One aggregated claims row. Every field is required — a partial row cannot be placed on the
    grain, and a missing count or amount is not the same as zero."""
    model_config = ConfigDict(use_enum_values=False)

    airline: str = Field(..., min_length=1, max_length=200,
                         description='Airline name as the claims sheet states it, e.g. "Emirates".')
    calendar_year: int = Field(..., ge=1950, le=2200, description="Calendar year the claims fall in.")
    number_of_claims: int = Field(..., ge=0, description="How many claims the row aggregates.")
    claim_amount: Decimal = Field(..., ge=0, description="Total amount, in `currency`. Not converted.")
    currency: ClaimCurrency = Field(..., description="USD, EUR or GBP.")
    policy_type: ClaimPolicyType = Field(..., description="HD, HSL, HW or WXS.")
    claims_status: ClaimsStatus = Field(..., description="Settled or Ongoing.")


class ClaimPatch(BaseModel):
    """A correction. Only the fields present in the body change. The grain columns may be edited too
    (a year or a currency typed wrong is exactly what needs fixing), which is why a PATCH can collide
    with an existing row — that comes back as 409, not a silent merge."""
    model_config = ConfigDict(use_enum_values=False)

    airline: Optional[str] = Field(None, min_length=1, max_length=200)
    calendar_year: Optional[int] = Field(None, ge=1950, le=2200)
    number_of_claims: Optional[int] = Field(None, ge=0)
    claim_amount: Optional[Decimal] = Field(None, ge=0)
    currency: Optional[ClaimCurrency] = None
    policy_type: Optional[ClaimPolicyType] = None
    claims_status: Optional[ClaimsStatus] = None


class ClaimBulk(BaseModel):
    rows: List[ClaimRow] = Field(..., min_length=1, max_length=5000,
                                 description="Rows to load, max 5000 per call.")
    upsert: bool = Field(True,
                         description="True (default): a row whose grain already exists is OVERWRITTEN "
                                     "with the new count and amount — this is what makes re-loading a "
                                     "corrected sheet safe. False: the whole call fails on the first "
                                     "collision and nothing is written.")


def _json(row: AcysClaims) -> dict:
    return {
        "id": row.id,
        "airline": row.airline,
        "calendar_year": row.calendar_year,
        "number_of_claims": row.number_of_claims,
        # NUMERIC in the column (exact cents), a plain number on the wire
        "claim_amount": float(row.claim_amount) if row.claim_amount is not None else None,
        "currency": row.currency.value if row.currency else None,
        "policy_type": row.policy_type.value if row.policy_type else None,
        "claims_status": row.claims_status.value if row.claims_status else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _conflict_msg(ex: IntegrityError) -> Optional[str]:
    """Turn a constraint violation into something the caller can act on, or None if it is not one of
    ours (in which case it is a 500, not a 409)."""
    text = str(getattr(ex, "orig", ex))
    if "uq_acys_claims_grain" in text:
        return ("A row already exists for this airline / year / currency / policy type / status. "
                "PATCH it, or load with upsert=true.")
    for ck, what in (("ck_acys_claims_count_non_negative", "number_of_claims cannot be negative"),
                     ("ck_acys_claims_amount_non_negative", "claim_amount cannot be negative"),
                     ("ck_acys_claims_year_sane", "calendar_year is outside 1950-2200")):
        if ck in text:
            return what
    return None


@router.get(
    "",
    description=(
        "List claims rows, newest year first. Every filter is optional and they AND together; "
        "`airline` matches case-insensitively on a substring so a partial name works. Use "
        "`year_from`/`year_to` for a range. The response carries the matching `total` alongside the "
        "page, so a grid can paginate without a second call."
    ),
    responses=build_responses(include={
        status.HTTP_200_OK, status.HTTP_400_BAD_REQUEST, status.HTTP_500_INTERNAL_SERVER_ERROR,
    }),
    dependencies=[Depends(authorize(SCOPE_PREDICTIVE_READ))],
)
async def list_claims(
    request: Request,
    response: Response,
    airline: Optional[str] = Query(None, description="Airline name substring, case-insensitive."),
    calendar_year: Optional[int] = Query(None, ge=1950, le=2200, description="Exact year."),
    year_from: Optional[int] = Query(None, ge=1950, le=2200, description="Range start, inclusive."),
    year_to: Optional[int] = Query(None, ge=1950, le=2200, description="Range end, inclusive."),
    currency: Optional[ClaimCurrency] = Query(None),
    policy_type: Optional[ClaimPolicyType] = Query(None),
    claims_status: Optional[ClaimsStatus] = Query(None),
    sort: str = Query("calendar_year", description=f"One of: {', '.join(sorted(_SORTABLE))}."),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    try:
        if sort not in _SORTABLE:
            return warning_response(
                request=request, response=response,
                msg=f"Unknown sort '{sort}'. Allowed: {', '.join(sorted(_SORTABLE))}",
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        if year_from is not None and year_to is not None and year_from > year_to:
            return warning_response(
                request=request, response=response,
                msg="year_from is later than year_to",
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        conds = []
        if airline:
            conds.append(AcysClaims.airline.ilike(f"%{airline.strip()}%"))
        if calendar_year is not None:
            conds.append(AcysClaims.calendar_year == calendar_year)
        if year_from is not None:
            conds.append(AcysClaims.calendar_year >= year_from)
        if year_to is not None:
            conds.append(AcysClaims.calendar_year <= year_to)
        if currency is not None:
            conds.append(AcysClaims.currency == currency)
        if policy_type is not None:
            conds.append(AcysClaims.policy_type == policy_type)
        if claims_status is not None:
            conds.append(AcysClaims.claims_status == claims_status)

        col = _SORTABLE[sort]
        col = col.desc() if order == "desc" else col.asc()
        stmt = (select(AcysClaims).where(*conds)
                # a stable tiebreak, so paging can't show the same row on two pages
                .order_by(col, AcysClaims.airline, AcysClaims.id)
                .limit(limit).offset(offset))

        async with request.app.state.db_client.session(_DB) as session:
            rows = (await session.execute(stmt)).scalars().all()
            total = await session.scalar(
                select(func.count()).select_from(AcysClaims).where(*conds))

        return success_response(
            request=request, response=response,
            data={"total": total, "limit": limit, "offset": offset,
                  "items": [_json(r) for r in rows]},
        )
    except Exception as ex:
        logger.error(f"list_claims failed: {ex}")
        return error_response(request=request, response=response, exc=ex)


@router.get(
    "/{claim_id}",
    description="One claims row by id.",
    responses=build_responses(include={
        status.HTTP_200_OK, status.HTTP_404_NOT_FOUND, status.HTTP_500_INTERNAL_SERVER_ERROR,
    }),
    dependencies=[Depends(authorize(SCOPE_PREDICTIVE_READ))],
)
async def get_claim(
    request: Request,
    response: Response,
    claim_id: int = Path(..., ge=1),
):
    try:
        async with request.app.state.db_client.session(_DB) as session:
            row = await session.get(AcysClaims, claim_id)
            if row is None:
                return warning_response(
                    request=request, response=response,
                    msg=f"Claims row {claim_id} not found",
                    status_code=status.HTTP_404_NOT_FOUND,
                )
            return success_response(request=request, response=response, data=_json(row))
    except Exception as ex:
        logger.error(f"get_claim failed: {ex}")
        return error_response(request=request, response=response, exc=ex)


@router.post(
    "",
    description=(
        "Add one claims row. Fails with 409 if a row already exists on the same grain "
        "(airline / year / currency / policy type / status) — to replace it, PATCH it or load "
        "through /bulk with upsert=true."
    ),
    responses=build_responses(include={
        status.HTTP_201_CREATED, status.HTTP_400_BAD_REQUEST, status.HTTP_409_CONFLICT,
        status.HTTP_500_INTERNAL_SERVER_ERROR,
    }),
    dependencies=[Depends(authorize(SCOPE_PREDICTIVE_WRITE))],
)
async def create_claim(request: Request, response: Response, body: ClaimRow):
    try:
        row = AcysClaims(
            airline=body.airline.strip(),
            calendar_year=body.calendar_year,
            number_of_claims=body.number_of_claims,
            claim_amount=body.claim_amount,
            currency=body.currency,
            policy_type=body.policy_type,
            claims_status=body.claims_status,
        )
        async with request.app.state.db_client.session(_DB) as session:
            session.add(row)
            try:
                await session.flush()
            except IntegrityError as ex:
                msg = _conflict_msg(ex)
                if msg is None:
                    raise
                await session.rollback()
                return warning_response(request=request, response=response, msg=msg,
                                        status_code=status.HTTP_409_CONFLICT)
            data = _json(row)
        return success_response(request=request, response=response, data=data,
                                status_code=status.HTTP_201_CREATED)
    except Exception as ex:
        logger.error(f"create_claim failed: {ex}")
        return error_response(request=request, response=response, exc=ex)


@router.post(
    "/bulk",
    description=(
        "Load many rows in ONE transaction — either all of them land or none do, so a rejected sheet "
        "never leaves a half-imported year behind. With `upsert=true` (default) a row whose grain "
        "already exists is overwritten with the new count and amount, which makes re-loading a "
        "corrected sheet safe and repeatable. Duplicate grains WITHIN one call are rejected outright "
        "rather than silently letting the last one win."
    ),
    responses=build_responses(include={
        status.HTTP_200_OK, status.HTTP_400_BAD_REQUEST, status.HTTP_409_CONFLICT,
        status.HTTP_500_INTERNAL_SERVER_ERROR,
    }),
    dependencies=[Depends(authorize(SCOPE_PREDICTIVE_WRITE))],
)
async def bulk_load_claims(request: Request, response: Response, body: ClaimBulk):
    try:
        values = []
        seen = {}
        for i, r in enumerate(body.rows):
            key = (r.airline.strip().lower(), r.calendar_year, r.currency,
                   r.policy_type, r.claims_status)
            if key in seen:
                return warning_response(
                    request=request, response=response,
                    msg=(f"Rows {seen[key]} and {i} are the same airline / year / currency / policy "
                         f"type / status. Combine them before loading."),
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            seen[key] = i
            values.append({
                "airline": r.airline.strip(),
                "calendar_year": r.calendar_year,
                "number_of_claims": r.number_of_claims,
                "claim_amount": r.claim_amount,
                "currency": r.currency,
                "policy_type": r.policy_type,
                "claims_status": r.claims_status,
            })

        stmt = pg_insert(AcysClaims).values(values)
        if body.upsert:
            stmt = stmt.on_conflict_do_update(
                constraint="uq_acys_claims_grain",
                set_={
                    "number_of_claims": stmt.excluded.number_of_claims,
                    "claim_amount": stmt.excluded.claim_amount,
                    "updated_at": func.now(),
                },
            )
        # RETURNING tells inserts from updates: a row whose id is also in the pre-existing set was
        # overwritten, so the caller learns what the load actually did instead of guessing.
        stmt = stmt.returning(AcysClaims.id, AcysClaims.created_at, AcysClaims.updated_at)

        async with request.app.state.db_client.session(_DB) as session:
            try:
                result = (await session.execute(stmt)).all()
            except IntegrityError as ex:
                msg = _conflict_msg(ex)
                if msg is None:
                    raise
                await session.rollback()
                return warning_response(request=request, response=response, msg=msg,
                                        status_code=status.HTTP_409_CONFLICT)
            updated = sum(1 for _id, created, upd in result if upd and created and upd > created)
            ids = [r[0] for r in result]

        return success_response(
            request=request, response=response,
            data={"received": len(values), "written": len(ids),
                  "inserted": len(ids) - updated, "updated": updated, "ids": ids},
            msg=f"Loaded {len(ids)} claims rows",
        )
    except Exception as ex:
        logger.error(f"bulk_load_claims failed: {ex}")
        return error_response(request=request, response=response, exc=ex)


@router.patch(
    "/{claim_id}",
    description=(
        "Correct one row. Only the fields present in the body change. Moving a row onto a grain that "
        "another row already occupies is refused with 409 — merge them yourself rather than having "
        "one silently absorb the other."
    ),
    responses=build_responses(include={
        status.HTTP_200_OK, status.HTTP_400_BAD_REQUEST, status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT, status.HTTP_500_INTERNAL_SERVER_ERROR,
    }),
    dependencies=[Depends(authorize(SCOPE_PREDICTIVE_WRITE))],
)
async def update_claim(
    request: Request,
    response: Response,
    body: ClaimPatch,
    claim_id: int = Path(..., ge=1),
):
    try:
        changes = body.model_dump(exclude_unset=True)
        if not changes:
            return warning_response(
                request=request, response=response,
                msg="Empty body: nothing to update",
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        if "airline" in changes and changes["airline"]:
            changes["airline"] = changes["airline"].strip()

        async with request.app.state.db_client.session(_DB) as session:
            row = await session.get(AcysClaims, claim_id)
            if row is None:
                return warning_response(
                    request=request, response=response,
                    msg=f"Claims row {claim_id} not found",
                    status_code=status.HTTP_404_NOT_FOUND,
                )
            for field, value in changes.items():
                setattr(row, field, value)
            try:
                await session.flush()
            except IntegrityError as ex:
                msg = _conflict_msg(ex)
                if msg is None:
                    raise
                await session.rollback()
                return warning_response(request=request, response=response, msg=msg,
                                        status_code=status.HTTP_409_CONFLICT)
            # `updated_at` is onupdate=now(), so the UPDATE leaves it expired on the instance.
            # Reading it back lazily is a MissingGreenlet under asyncio — refresh explicitly, as
            # Routers/Claims.py does after its own update.
            await session.refresh(row)
            data = _json(row)
        return success_response(request=request, response=response, data=data)
    except Exception as ex:
        logger.error(f"update_claim failed: {ex}")
        return error_response(request=request, response=response, exc=ex)


@router.delete(
    "/{claim_id}",
    description=(
        "Delete one row. The deleted row comes back in full, so it is complete enough to re-POST if "
        "it turns out the wrong id was sent."
    ),
    responses=build_responses(include={
        status.HTTP_200_OK, status.HTTP_404_NOT_FOUND, status.HTTP_500_INTERNAL_SERVER_ERROR,
    }),
    dependencies=[Depends(authorize(SCOPE_PREDICTIVE_WRITE))],
)
async def delete_claim(
    request: Request,
    response: Response,
    claim_id: int = Path(..., ge=1),
):
    try:
        async with request.app.state.db_client.session(_DB) as session:
            row = await session.get(AcysClaims, claim_id)
            if row is None:
                return warning_response(
                    request=request, response=response,
                    msg=f"Claims row {claim_id} not found",
                    status_code=status.HTTP_404_NOT_FOUND,
                )
            data = _json(row)
            await session.execute(sa_delete(AcysClaims).where(AcysClaims.id == claim_id))
        return success_response(request=request, response=response, data={"deleted": data})
    except Exception as ex:
        logger.error(f"delete_claim failed: {ex}")
        return error_response(request=request, response=response, exc=ex)
