"""Airline claims experience (forecast.acys_claims) — the aggregated claims sheet, per airline/year.

One row is "this airline had N claims worth X in this calendar year under this section of cover, of
which Y is still outstanding". It is loaded as stated (a broker's claims-experience summary), NOT
derived from individual loss events.

NOT a per-aircraft loss record. The `insurance` schema that held those was dropped whole in
revision `insured_fleet_rebuild`, and this router never depended on it: `/forecast/claims` serves a
reporting input for the forecast model, nothing operational.

The grain is
    airline x calendar_year x currency x policy_type
so two currencies are separate rows for the same year. Neither amount is ever summed across
currencies; the `_usd` pair is what may be.

SETTLED VS OUTSTANDING IS NOT A ROW SPLIT. It used to be a `claims_status` column, so one year's
experience arrived as two rows that a reader had to add back together. It is now two columns of ONE
row: `claims_amount_total` is everything the year cost, `claims_amount_outstanding` the part of it
still moving as a reserve. The settled figure is the difference. The two are deliberately not
constrained against each other — a sheet occasionally states an outstanding figure above its total,
and this table's contract is "as stated".

DUPLICATES ARE KEPT. The grain is not unique: the same combination may appear as many times as it is
sent, and nothing here merges, replaces or rejects it. A sheet may state a combination twice for its
own reasons, and this loader is not the place to decide which one is real. The consequence to know
before re-running a load: sending the same sheet twice stores it twice — there is no upsert, because
without a unique key there is nothing for one to target.

TWO THINGS HAPPEN ON THE WAY IN, and the second is why a load can be rejected:

1. THE AIRLINE NAME IS RESOLVED against cirium.airlines — the same reference /airlines searches —
   with fuzzy matching, so "Corendon Airlnes Europe" is stored as "Corendon Airlines Europe". Hand-
   made sheets spell the same carrier several ways, and storing them verbatim splits one airline
   across rows that no per-airline total will ever bring back together. A name that cannot be
   resolved CONFIDENTLY is never guessed at — it is stored EXACTLY AS SENT and reported back with
   its near misses, so an unknown carrier (or one the reference spells three ways at once) costs a
   flag in the response, not a rejected sheet. See Utils/AirlineResolver.py for why a threshold
   alone is not enough.

2. BOTH AMOUNTS ARE CONVERTED TO USD, AT THE ROW'S OWN YEAR'S RATE. A 2019 row is converted at the 2019
   close-of-year rate, not today's — converting old claims at a current rate restates history by
   tens of percent. Only a row in the current year uses the latest published rate, since its
   31 December has not happened yet. The rate goes in `currency_rate`, and each amount times that
   rate in `claims_amount_total_usd` / `claims_amount_outstanding_usd` — ONE rate for both, so a row
   can never hold two figures booked at different rates. USD rows get a rate of exactly 1 and the
   amounts unchanged, so every row has the same shape. A year with no published series (before 1999)
   and an unreachable FX source both refuse the write rather than storing it unconverted.

    GET    /forecast/claims           list, filtered (airline, year range, currency, policy type)
    GET    /forecast/claims/{id}      one row
    POST   /forecast/claims           add one row
    POST   /forecast/claims/bulk      load many rows in one transaction (all of them, duplicates included)
    PATCH  /forecast/claims/{id}      correct a row (re-resolves and re-converts what it touches)

THERE IS NO DELETE. A row is corrected, never removed: this table is the claims history a report
reads, and a year that quietly disappears from it is indistinguishable from a year that had no
claims. A row entered by mistake is edited to what it should have been.

PowerBI reads the table (or a service-side filtered slice of it) directly as `bi_reader`; this router
is the write path and the ad-hoc read path.
"""
from collections import Counter
from decimal import Decimal
from typing import Optional, List

from fastapi import Request, Response, Depends, Query, Path, status
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from Config import setup_logger
from settings import Router
from Database.ForecastModels import AcysClaims, ClaimCurrency, ClaimPolicyType
from api_auth import authorize, SCOPE_PREDICTIVE_READ, SCOPE_PREDICTIVE_WRITE
from Utils import success_response, warning_response, error_response
from Utils.ResponsesFunc import build_responses
from Utils.AirlineResolver import resolve_airline, resolve_airlines
from Utils.CurrencyRates import RateUnavailable, get_usd_rate, get_usd_rates, to_usd

logger = setup_logger("acys_claims_api")

router = Router(prefix="/forecast/claims", tags=["Forecast"])

_DB = "aixii"

_SORTABLE = {
    "airline": AcysClaims.airline,
    "calendar_year": AcysClaims.calendar_year,
    "number_of_claims": AcysClaims.number_of_claims,
    "claims_amount_total": AcysClaims.claims_amount_total,
    "claims_amount_outstanding": AcysClaims.claims_amount_outstanding,
    "currency": AcysClaims.currency,
    "policy_type": AcysClaims.policy_type,
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
    claims_amount_total: Decimal = Field(
        ..., ge=0, description="Everything the year cost, in `currency`. Not converted.")
    claims_amount_outstanding: Decimal = Field(
        ..., ge=0, description="The part of the total still moving as a reserve, in `currency`. The "
                               "settled figure is the difference of the two. Send 0 for a year that "
                               "is fully settled.")
    currency: ClaimCurrency = Field(..., description="USD, EUR or GBP.")
    policy_type: ClaimPolicyType = Field(..., description="HD, HSL, HW or WXS.")


class ClaimPatch(BaseModel):
    """A correction. Only the fields present in the body change, and the grain columns may be edited
    too (a year or a currency typed wrong is exactly what needs fixing). Landing on a grain another
    row already holds is fine — duplicates are kept."""
    model_config = ConfigDict(use_enum_values=False)

    airline: Optional[str] = Field(None, min_length=1, max_length=200)
    calendar_year: Optional[int] = Field(None, ge=1950, le=2200)
    number_of_claims: Optional[int] = Field(None, ge=0)
    claims_amount_total: Optional[Decimal] = Field(None, ge=0)
    claims_amount_outstanding: Optional[Decimal] = Field(None, ge=0)
    currency: Optional[ClaimCurrency] = None
    policy_type: Optional[ClaimPolicyType] = None


class ClaimBulk(BaseModel):
    """A batch to load. Every row is inserted, including rows that repeat a grain already in the
    table or repeated within this batch — duplicates are kept as sent."""
    rows: List[ClaimRow] = Field(..., min_length=1, max_length=5000,
                                 description="Rows to load, max 5000 per call. Duplicates are kept.")


def _json(row: AcysClaims) -> dict:
    return {
        "id": row.id,
        "airline": row.airline,
        "calendar_year": row.calendar_year,
        "number_of_claims": row.number_of_claims,
        # NUMERIC in the column (exact cents), a plain number on the wire
        "claims_amount_total": (float(row.claims_amount_total)
                                if row.claims_amount_total is not None else None),
        "claims_amount_outstanding": (float(row.claims_amount_outstanding)
                                      if row.claims_amount_outstanding is not None else None),
        "currency_rate": float(row.currency_rate) if row.currency_rate is not None else None,
        "claims_amount_total_usd": (float(row.claims_amount_total_usd)
                                    if row.claims_amount_total_usd is not None else None),
        "claims_amount_outstanding_usd": (float(row.claims_amount_outstanding_usd)
                                          if row.claims_amount_outstanding_usd is not None else None),
        "currency": row.currency.value if row.currency else None,
        "policy_type": row.policy_type.value if row.policy_type else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _conflict_msg(ex: IntegrityError) -> Optional[str]:
    """Turn a constraint violation into something the caller can act on, or None if it is not one of
    ours (in which case it is a 500, not a 409)."""
    text = str(getattr(ex, "orig", ex))
    for ck, what in (("ck_acys_claims_count_non_negative", "number_of_claims cannot be negative"),
                     ("ck_acys_claims_amount_non_negative",
                      "claims_amount_total cannot be negative"),
                     ("ck_acys_claims_outstanding_non_negative",
                      "claims_amount_outstanding cannot be negative"),
                     ("ck_acys_claims_year_sane", "calendar_year is outside 1950-2200"),
                     ("ck_acys_claims_usd_pair_complete",
                      "currency_rate and both converted amounts must be set together"),
                     ("ck_acys_claims_rate_positive", "currency_rate must be positive"),
                     ("ck_acys_claims_usd_non_negative",
                      "claims_amount_total_usd cannot be negative"),
                     ("ck_acys_claims_usd_outstanding_non_negative",
                      "claims_amount_outstanding_usd cannot be negative")):
        if ck in text:
            return what
    return None


def _unresolved_msg(match) -> str:
    """Explain a name the reference could not settle and show what it nearly matched. The row is
    written anyway, under the name as sent — this is the flag that says so, so a caller comparing
    their sheet against the reference can fix the spelling later instead of losing the load now."""
    near = f" Nearest reference names: {', '.join(match.candidates[:3])}." if match.candidates else ""
    return (f"airline '{match.typed}' is not in the reference data ({match.reason}) — "
            f"stored as sent.{near}")


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

        col = _SORTABLE[sort]
        col = col.desc() if order == "desc" else col.asc()
        stmt = (select(AcysClaims).where(*conds)
                # a stable tiebreak, so paging can't show the same row on two pages
                .order_by(col, AcysClaims.airline, AcysClaims.id)
                .limit(limit).offset(offset))

        async with request.app.state.db_client.read_session(_DB) as session:
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
        async with request.app.state.db_client.read_session(_DB) as session:
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
        "Add one claims row. The airline name is resolved against the reference data first (a "
        "one-character misspelling still lands on the right carrier; the stored name is the "
        "reference spelling, and the response reports the correction), and BOTH amounts are "
        "converted to USD at the rate of the row's OWN calendar year (the latest rate only for a row "
        "in the current year). A row repeating one already in the table is stored as another "
        "row — duplicates are kept, not merged. An airline the reference cannot settle is stored "
        "exactly as sent and flagged in the response; only a missing FX rate refuses the write (502)."
    ),
    responses=build_responses(include={
        status.HTTP_201_CREATED, status.HTTP_409_CONFLICT,
        status.HTTP_502_BAD_GATEWAY, status.HTTP_500_INTERNAL_SERVER_ERROR,
    }),
    dependencies=[Depends(authorize(SCOPE_PREDICTIVE_WRITE))],
)
async def create_claim(request: Request, response: Response, body: ClaimRow):
    try:
        async with request.app.state.db_client.session(_DB) as session:
            # An unresolved name is stored as sent (match.stored), never refused: see
            # Utils/AirlineResolver.AirlineMatch.stored. It comes back flagged in the response.
            match = await resolve_airline(session, body.airline)

            try:
                rate = await get_usd_rate(getattr(request.state, "redis", None),
                                          body.currency.value, body.calendar_year)
            except RateUnavailable as ex:
                logger.error(f"create_claim: FX unavailable: {ex}")
                return warning_response(
                    request=request, response=response,
                    msg=f"Could not obtain a {body.currency.value}->USD rate for "
                        f"{body.calendar_year}, so the row was not written: {ex}",
                    status_code=status.HTTP_502_BAD_GATEWAY,
                )

            row = AcysClaims(
                airline=match.stored,
                calendar_year=body.calendar_year,
                number_of_claims=body.number_of_claims,
                claims_amount_total=body.claims_amount_total,
                claims_amount_outstanding=body.claims_amount_outstanding,
                currency=body.currency,
                policy_type=body.policy_type,
                currency_rate=rate,
                claims_amount_total_usd=to_usd(body.claims_amount_total, rate),
                claims_amount_outstanding_usd=to_usd(body.claims_amount_outstanding, rate),
            )
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
            if match.changed:
                # surfaced, not silent: the caller sees that the name they sent was corrected
                data["airline_resolved_from"] = match.typed
            elif match.resolved is None:
                data["airline_unresolved"] = _unresolved_msg(match)
        return success_response(
            request=request, response=response, data=data,
            msg=(f"Airline resolved to '{match.resolved}'" if match.changed
                 else _unresolved_msg(match) if match.resolved is None else "Success"),
            status_code=status.HTTP_201_CREATED)
    except Exception as ex:
        logger.error(f"create_claim failed: {ex}")
        return error_response(request=request, response=response, exc=ex)


@router.post(
    "/bulk",
    description=(
        "Load many rows in ONE transaction — either all of them land or none do, so a rejected sheet "
        "never leaves a half-imported year behind. Every airline name is resolved against the "
        "reference data and both amounts converted to USD — each row at its own calendar year's "
        "rate — before anything is written; only an unavailable rate rejects the whole batch, while "
        "a name the reference cannot settle is stored exactly as sent and listed back under "
        "`airlines_unresolved`. EVERY row is then "
        "inserted, including rows repeating a grain already in the table or repeated within the "
        "batch — duplicates are kept as sent, nothing is merged or overwritten. Re-sending the same "
        "sheet therefore stores it a second time."
    ),
    responses=build_responses(include={
        status.HTTP_200_OK, status.HTTP_409_CONFLICT,
        status.HTTP_502_BAD_GATEWAY, status.HTTP_500_INTERNAL_SERVER_ERROR,
    }),
    dependencies=[Depends(authorize(SCOPE_PREDICTIVE_WRITE))],
)
async def bulk_load_claims(request: Request, response: Response, body: ClaimBulk):
    try:
        async with request.app.state.db_client.session(_DB) as session:
            # One lookup per DISTINCT spelling and per DISTINCT currency, not per row: a sheet is
            # hundreds of rows over a handful of carriers and two or three currencies.
            matches = await resolve_airlines(session, [r.airline for r in body.rows])

            try:
                # one lookup per (currency, year) the batch actually contains
                rates = await get_usd_rates(
                    getattr(request.state, "redis", None),
                    {(r.currency.value, r.calendar_year) for r in body.rows})
            except RateUnavailable as ex:
                logger.error(f"bulk_load_claims: FX unavailable: {ex}")
                return warning_response(
                    request=request, response=response,
                    msg=f"Could not obtain an FX rate, so nothing was written: {ex}",
                    status_code=status.HTTP_502_BAD_GATEWAY,
                )

            # Every row is inserted, in the order it was sent. Rows repeating a grain — already in
            # the table, or twice within this batch — are NOT merged or rejected: duplicates are
            # kept, so the stored set is exactly what was sent.
            values = []
            for r in body.rows:
                rate = rates[(r.currency.value, r.calendar_year)]
                values.append({
                    "airline": matches[r.airline].stored,
                    "calendar_year": r.calendar_year,
                    "number_of_claims": r.number_of_claims,
                    "claims_amount_total": r.claims_amount_total,
                    "claims_amount_outstanding": r.claims_amount_outstanding,
                    "currency": r.currency,
                    "policy_type": r.policy_type,
                    "currency_rate": rate,
                    "claims_amount_total_usd": to_usd(r.claims_amount_total, rate),
                    "claims_amount_outstanding_usd": to_usd(r.claims_amount_outstanding, rate),
                })

            stmt = pg_insert(AcysClaims).values(values).returning(AcysClaims.id)

            try:
                result = (await session.execute(stmt)).all()
            except IntegrityError as ex:
                msg = _conflict_msg(ex)
                if msg is None:
                    raise
                await session.rollback()
                return warning_response(request=request, response=response, msg=msg,
                                        status_code=status.HTTP_409_CONFLICT)
            ids = [r[0] for r in result]

        # Report the corrections rather than applying them silently: a caller comparing their sheet
        # against what was stored needs to know which names the reference data changed.
        corrections = [{"typed": m.typed, "resolved": m.resolved}
                       for m in matches.values() if m.changed]
        # ...and the names the reference could not settle, which went in as sent. Reported per
        # DISTINCT spelling, not per row: a sheet repeats the same carrier hundreds of times and a
        # per-row list would be hundreds of identical paragraphs.
        rows_per_name = Counter(r.airline for r in body.rows)
        unresolved = [{"typed": m.typed, "reason": m.reason, "rows": rows_per_name[m.typed],
                       "candidates": m.candidates[:3], "note": _unresolved_msg(m)}
                      for m in matches.values() if m.resolved is None]
        return success_response(
            request=request, response=response,
            data={"received": len(values), "inserted": len(ids), "ids": ids,
                  "airlines_resolved": corrections,
                  "airlines_unresolved": unresolved,
                  "rates_used": [{"currency": cur, "calendar_year": yr, "rate": float(rate)}
                                 for (cur, yr), rate in sorted(rates.items())]},
            msg=(f"Loaded {len(ids)} claims rows"
                 + (f"; {len(corrections)} airline name(s) resolved to the reference spelling"
                    if corrections else "")
                 + (f"; {len(unresolved)} airline name(s) not in the reference data, stored as sent"
                    if unresolved else "")),
        )
    except Exception as ex:
        logger.error(f"bulk_load_claims failed: {ex}")
        return error_response(request=request, response=response, exc=ex)


@router.patch(
    "/{claim_id}",
    description=(
        "Correct one row. Only the fields present in the body change. A new airline name goes "
        "through the same resolution as a load — one the reference cannot settle is stored as "
        "sent and flagged — and changing EITHER amount, the currency OR the "
        "calendar year RE-CONVERTS both amounts at that year's rate — otherwise the stored USD "
        "figures would keep converting a number, or a year, that is no longer there. Moving a row "
        "onto a grain another row already occupies is allowed: duplicates are kept. There is no "
        "delete: a row is corrected, never removed."
    ),
    responses=build_responses(include={
        status.HTTP_200_OK, status.HTTP_400_BAD_REQUEST, status.HTTP_404_NOT_FOUND,
        status.HTTP_502_BAD_GATEWAY, status.HTTP_500_INTERNAL_SERVER_ERROR,
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
        async with request.app.state.db_client.session(_DB) as session:
            row = await session.get(AcysClaims, claim_id)
            if row is None:
                return warning_response(
                    request=request, response=response,
                    msg=f"Claims row {claim_id} not found",
                    status_code=status.HTTP_404_NOT_FOUND,
                )

            resolved_note = unresolved_note = None
            if changes.get("airline"):
                match = await resolve_airline(session, changes["airline"])
                changes["airline"] = match.stored   # as sent when the reference cannot settle it
                if match.changed:
                    resolved_note = match
                elif match.resolved is None:
                    unresolved_note = _unresolved_msg(match)

            # The conversion is a function of (amounts, currency, year) — the year included,
            # because the rate is that year's. Touch any of them and the stored set is stale, so it
            # is recomputed exactly as a fresh load would have computed it. BOTH amounts are
            # recomputed even when only one changed: they share one rate, and re-reading the
            # untouched one from the row keeps the pair consistent either way.
            if {"claims_amount_total", "claims_amount_outstanding",
                    "currency", "calendar_year"} & set(changes):
                new_currency = changes.get("currency", row.currency)
                new_total = changes.get("claims_amount_total", row.claims_amount_total)
                new_outstanding = changes.get("claims_amount_outstanding",
                                              row.claims_amount_outstanding)
                new_year = changes.get("calendar_year", row.calendar_year)
                try:
                    rate = await get_usd_rate(getattr(request.state, "redis", None),
                                              new_currency.value, new_year)
                except RateUnavailable as ex:
                    logger.error(f"update_claim: FX unavailable: {ex}")
                    return warning_response(
                        request=request, response=response,
                        msg=f"Could not obtain a {new_currency.value}->USD rate for {new_year}, so "
                            f"the row was not changed: {ex}",
                        status_code=status.HTTP_502_BAD_GATEWAY,
                    )
                changes["currency_rate"] = rate
                changes["claims_amount_total_usd"] = to_usd(new_total, rate)
                changes["claims_amount_outstanding_usd"] = to_usd(new_outstanding, rate)

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
            if resolved_note is not None:
                data["airline_resolved_from"] = resolved_note.typed
            elif unresolved_note is not None:
                data["airline_unresolved"] = unresolved_note
        return success_response(
            request=request, response=response, data=data,
            msg=(f"Airline resolved to '{resolved_note.resolved}'" if resolved_note is not None
                 else unresolved_note if unresolved_note is not None else "Success"))
    except Exception as ex:
        logger.error(f"update_claim failed: {ex}")
        return error_response(request=request, response=response, exc=ex)
