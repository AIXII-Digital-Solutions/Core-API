"""Predictive-utilisation endpoints (stage 1).

- GET  /predictive_utilisation/airlines  — sorted airline list from the cirium.airlines matview.
- POST /predictive_utilisation/           — resolve the airline (name | ICAO | IATA) and enqueue the
  external-worker `predictive_utilisation` job (steps 3–6); returns the ARQ job_id to poll /status.

The result read-back (api.predictive_utilisation) and the 3 step outputs are a later stage.
"""
from datetime import date as date_cls
from typing import Optional

from fastapi import Request, Response, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select, or_, func, desc

from Config import setup_logger
from settings import Router
from Queue import EXTERNAL_QUEUE
from Database.CiriumModels import CiriumAirlines
from api_auth import authorize, SCOPE_PREDICTIVE_READ, SCOPE_PREDICTIVE_WRITE
from Utils import success_response, error_response
from Utils.ResponsesFunc import build_responses, warning_response

logger = setup_logger("predictive_utilisation_api")

router = Router(prefix="/predictive_utilisation", tags=["Predictive Utilisation"])


class PredictRequest(BaseModel):
    airline: str = Field(..., description="Airline name OR ICAO/IATA code (resolved via cirium.airlines)")
    date: date_cls = Field(..., description="Reference date, ISO YYYY-MM-DD. Window = [date-2y, date].")


@router.get(
    path="/airlines",
    description=(
        "Typeahead search over cirium.airlines: name substring OR ICAO/IATA prefix. "
        "Empty `q` returns an empty list (dropdown stays empty until the user types). "
        "Only airlines that HAVE an ICAO code (usable in predictive) are returned. "
        "Results are capped by `limit` and ranked name-prefix-first, then alphabetically."
    ),
    responses=build_responses(include={status.HTTP_200_OK, status.HTTP_500_INTERNAL_SERVER_ERROR}),
    dependencies=[Depends(authorize(SCOPE_PREDICTIVE_READ))],
)
async def search_airlines(
    request: Request,
    response: Response,
    q: str = Query("", description="Search text: airline name substring, or ICAO/IATA prefix."),
    limit: int = Query(10, ge=1, le=50, description="Max results returned."),
):
    try:
        q = q.strip()
        if not q:
            return success_response(request=request, response=response, data=[])

        pattern = f"%{q}%"     # name: contains
        prefix = f"{q}%"       # codes (and prefix-ranking): starts-with
        stmt = (
            select(CiriumAirlines)
            .where(
                CiriumAirlines.icao.isnot(None),   # only airlines usable in predictive (ICAO required)
                or_(
                    CiriumAirlines.airline.ilike(pattern),
                    CiriumAirlines.icao.ilike(prefix),
                    CiriumAirlines.iata.ilike(prefix),
                ),
            )
            .order_by(
                desc(CiriumAirlines.airline.ilike(prefix)),   # name-prefix matches first
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


@router.post(
    path="/",
    description="Start the predictive-utilisation pipeline for an airline + reference date.",
    status_code=status.HTTP_202_ACCEPTED,
    responses=build_responses(include={
        status.HTTP_202_ACCEPTED, status.HTTP_404_NOT_FOUND,
        status.HTTP_422_UNPROCESSABLE_ENTITY, status.HTTP_500_INTERNAL_SERVER_ERROR,
    }),
    dependencies=[Depends(authorize(SCOPE_PREDICTIVE_WRITE))],
)
async def start_predictive(body: PredictRequest, request: Request, response: Response):
    try:
        # resolve name | ICAO | IATA -> (icao, iata) via cirium.airlines
        async with request.app.state.db_client.session("aixii") as session:
            stmt = (
                select(CiriumAirlines)
                .where(or_(
                    func.lower(CiriumAirlines.airline) == body.airline.strip().lower(),
                    CiriumAirlines.icao == body.airline.strip(),
                    CiriumAirlines.iata == body.airline.strip(),
                ))
                .limit(1)
            )
            row = (await session.execute(stmt)).scalars().first()

        if row is None:
            return warning_response(request=request, response=response,
                                    msg=f"Airline '{body.airline}' not found in cirium.airlines",
                                    status_code=status.HTTP_404_NOT_FOUND)
        if not row.icao:
            return warning_response(request=request, response=response,
                                    msg=f"Airline '{row.airline}' has no ICAO code — cannot fetch flights",
                                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY)

        job = await request.state.arq.enqueue_job(
            "predictive_utilisation",
            icao=row.icao,
            iata=row.iata,
            date=body.date.isoformat(),
            correlation_id=request.state.correlation_id,
            _queue_name=EXTERNAL_QUEUE,
        )
        return success_response(
            request=request, response=response,
            data={"job_id": job.job_id, "icao": row.icao, "iata": row.iata, "airline": row.airline},
            msg="Predictive utilisation started", status_code=status.HTTP_202_ACCEPTED,
        )
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)
