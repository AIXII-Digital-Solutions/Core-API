"""Registration reference search (typeahead) over the cirium.registrations materialized view.

Analogue of /airlines, but for tail numbers. Each row is a unique Registration with its LATEST
Operator + Status. Instead of the airlines' `icao_only`, this takes `active_only` (default true):
when true it keeps only aircraft whose Status is operationally active — "In Service" or "Storage" —
excluding retired / written-off / on-order / cancelled / etc. Empty `q` returns an empty list;
registration-prefix matches rank first, then alphabetical; capped by `limit`.
"""
from fastapi import Request, Response, Depends, Query, status
from sqlalchemy import select, desc

from Config import setup_logger
from settings import Router
from Database.CiriumModels import CiriumRegistrations
from api_auth import authorize, SCOPE_PREDICTIVE_READ
from Utils import success_response, error_response
from Utils.ResponsesFunc import build_responses

logger = setup_logger("registrations_api")

router = Router(prefix="/registrations", tags=["Registrations"])

# Operationally "active" statuses — the only ones kept when active_only=true. Exact Cirium strings.
_ACTIVE_STATUSES = ["In Service", "Storage"]


@router.get(
    path="/",
    description=(
        "Typeahead search over cirium.registrations: registration substring. Empty `q` returns an "
        "empty list. `active_only` (default true) keeps only operationally active aircraft "
        "(Status 'In Service' or 'Storage'), excluding retired/written-off/on-order/cancelled/etc.; "
        "set false to search all. Prefix matches rank first, then alphabetical; capped by `limit`."
    ),
    responses=build_responses(include={status.HTTP_200_OK, status.HTTP_500_INTERNAL_SERVER_ERROR}),
    dependencies=[Depends(authorize(SCOPE_PREDICTIVE_READ))],
)
async def search_registrations(
    request: Request,
    response: Response,
    q: str = Query("", description="Search text: registration substring."),
    limit: int = Query(10, ge=1, le=50, description="Max results returned."),
    active_only: bool = Query(True, description="If true (default), only 'In Service' / 'Storage' aircraft."),
):
    try:
        q = q.strip()
        if not q:
            return success_response(request=request, response=response, data=[])

        pattern = f"%{q}%"     # contains
        prefix = f"{q}%"       # starts-with (for ranking)
        conds = [CiriumRegistrations.registration.ilike(pattern)]
        if active_only:
            conds.append(CiriumRegistrations.status.in_(_ACTIVE_STATUSES))

        stmt = (
            select(CiriumRegistrations)
            .where(*conds)
            .order_by(
                desc(CiriumRegistrations.registration.ilike(prefix)),   # prefix matches first
                CiriumRegistrations.registration,                        # then alphabetical
            )
            .limit(limit)
        )
        async with request.app.state.db_client.session("aixii") as session:
            rows = (await session.execute(stmt)).scalars().all()
        data = [{"registration": r.registration, "operator": r.operator, "status": r.status} for r in rows]
        return success_response(request=request, response=response, data=data)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)
