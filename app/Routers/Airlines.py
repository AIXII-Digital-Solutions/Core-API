"""Airlines reference search (typeahead) over the cirium.airlines materialized view.

Its own `/airlines` group. By default searches ALL airlines; pass `icao_only=true` to restrict to
airlines that have an ICAO code (the ones usable in predictive — its POST needs an ICAO). Empty `q`
returns an empty list. Coded airlines are ranked first, then name-prefix matches, then alphabetical;
the result set is capped by `limit`.
"""
from fastapi import Request, Response, Depends, Query, status
from sqlalchemy import select, or_, desc

from Config import setup_logger
from settings import Router
from Database.CiriumModels import CiriumAirlines
from api_auth import authorize, SCOPE_PREDICTIVE_READ
from Utils import success_response, error_response
from Utils.ResponsesFunc import build_responses

logger = setup_logger("airlines_api")

router = Router(prefix="/airlines", tags=["Airlines"])


@router.get(
    path="/",
    description=(
        "Typeahead search over cirium.airlines: name substring OR ICAO/IATA prefix. "
        "Empty `q` returns an empty list. `icao_only` (default false) searches all airlines; set true "
        "to return only airlines that HAVE an ICAO code (usable in predictive). Ranked coded-first, "
        "then name-prefix, then alphabetically; capped by `limit`."
    ),
    responses=build_responses(include={status.HTTP_200_OK, status.HTTP_500_INTERNAL_SERVER_ERROR}),
    dependencies=[Depends(authorize(SCOPE_PREDICTIVE_READ))],
)
async def search_airlines(
    request: Request,
    response: Response,
    q: str = Query("", description="Search text: airline name substring, or ICAO/IATA prefix."),
    limit: int = Query(10, ge=1, le=50, description="Max results returned."),
    icao_only: bool = Query(False, description="If true, return only airlines that have an ICAO code."),
    all_on_empty: bool = Query(False, description="If true, an empty query returns the first N airlines."),
):
    try:
        q = q.strip()
        if not q:
            if not all_on_empty:
                return success_response(request=request, response=response, data=[])
            conds = [CiriumAirlines.icao.isnot(None)] if icao_only else []
            stmt = (
                select(CiriumAirlines).where(*conds)
                .order_by(
                    desc(or_(CiriumAirlines.icao.isnot(None), CiriumAirlines.iata.isnot(None))),
                    CiriumAirlines.airline,
                )
                .limit(limit)
            )
            async with request.app.state.db_client.session("aixii") as session:
                rows = (await session.execute(stmt)).scalars().all()
            data = [{"airline": r.airline, "icao": r.icao, "iata": r.iata} for r in rows]
            return success_response(request=request, response=response, data=data)

        pattern = f"%{q}%"     # name: contains
        prefix = f"{q}%"       # codes (and prefix-ranking): starts-with
        conds = [or_(
            CiriumAirlines.airline.ilike(pattern),
            CiriumAirlines.icao.ilike(prefix),
            CiriumAirlines.iata.ilike(prefix),
        )]
        if icao_only:
            conds.append(CiriumAirlines.icao.isnot(None))

        stmt = (
            select(CiriumAirlines)
            .where(*conds)
            .order_by(
                # coded (real) airlines first — no-code rows sink to the bottom
                desc(or_(CiriumAirlines.icao.isnot(None), CiriumAirlines.iata.isnot(None))),
                desc(CiriumAirlines.airline.ilike(prefix)),   # then name-prefix matches
                CiriumAirlines.airline,                        # then alphabetical
            )
            .limit(limit)
        )
        async with request.app.state.db_client.session("aixii") as session:
            rows = (await session.execute(stmt)).scalars().all()
        data = [{"airline": r.airline, "icao": r.icao, "iata": r.iata} for r in rows]
        return success_response(request=request, response=response, data=data)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)
