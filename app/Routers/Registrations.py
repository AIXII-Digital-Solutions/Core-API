"""Registration reference search (typeahead) over the cirium.registrations materialized view.

Analogue of /airlines, for tail numbers. Each row is a unique Registration with its LATEST Operator +
Status. Search is separator-insensitive: "YLLTD" matches "YL-LTD" (matched on registration_norm =
upper, non-alphanumerics stripped). Filters:
  * q            — registration substring (normalized); prefix matches rank first.
  * operator     — operator-name SUBSTRING (ILIKE) -> all matching operators' registrations (combine with q).
  * active_only  — keep only operationally active aircraft (Status 'In Service' / 'Storage').
  * all_on_empty — when true, an empty search (no q, no operator) returns the first N rows instead of [].
"""
from typing import Optional

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


def _normalize(s: str) -> str:
    """upper + strip non-alphanumerics — matches the matview's registration_norm."""
    return "".join(ch for ch in s.upper() if ch.isalnum())


@router.get(
    path="/",
    description=(
        "Typeahead over cirium.registrations. `q` = registration substring, separator-insensitive "
        "('YLLTD' matches 'YL-LTD'). `operator` = operator-name substring -> all matching operators' "
        "tails (combine with q to narrow). `active_only` (default true) keeps only 'In Service' / 'Storage'. "
        "`all_on_empty` (default false): an empty search returns the first N instead of []. "
        "Prefix matches rank first, then alphabetical; capped by `limit`."
    ),
    responses=build_responses(include={status.HTTP_200_OK, status.HTTP_500_INTERNAL_SERVER_ERROR}),
    dependencies=[Depends(authorize(SCOPE_PREDICTIVE_READ))],
)
async def search_registrations(
    request: Request,
    response: Response,
    q: str = Query("", description="Registration substring (separator-insensitive)."),
    operator: Optional[str] = Query(None, description="Operator name substring -> all matching operators' registrations."),
    limit: int = Query(10, ge=1, le=50, description="Max results returned."),
    active_only: bool = Query(True, description="If true (default), only 'In Service' / 'Storage'."),
    all_on_empty: bool = Query(False, description="If true, an empty search returns the first N rows."),
):
    try:
        nq = _normalize(q.strip()) if q else ""
        operator = operator.strip() if operator else None
        has_search = bool(nq) or bool(operator)

        conds = []
        order = [CiriumRegistrations.registration]
        if active_only:
            conds.append(CiriumRegistrations.status.in_(_ACTIVE_STATUSES))
        if operator:
            conds.append(CiriumRegistrations.operator.ilike(f"%{operator}%"))
        if nq:
            conds.append(CiriumRegistrations.registration_norm.like(f"%{nq}%"))
            order = [desc(CiriumRegistrations.registration_norm.like(f"{nq}%")),  # prefix first
                     CiriumRegistrations.registration]

        if not has_search and not all_on_empty:
            return success_response(request=request, response=response, data=[])

        stmt = select(CiriumRegistrations).where(*conds).order_by(*order).limit(limit)
        async with request.app.state.db_client.session("aixii") as session:
            rows = (await session.execute(stmt)).scalars().all()
        data = [{"registration": r.registration, "operator": r.operator, "status": r.status} for r in rows]
        return success_response(request=request, response=response, data=data)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)
