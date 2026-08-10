"""Reference lookups behind the insurance forms' autocomplete.

The write endpoints take names, not ids, and find-or-create the reference row (matching on the
STORED generated `*_normalized` columns). These endpoints are what lets the portal offer the names
that ALREADY exist, so a user picks `AerCap` from a list instead of typing `AerCap ` and relying on
normalisation to save them.

Ranking is the same everywhere: prefix matches first, then anything containing the text, then
alphabetically. An empty `q` returns the first page alphabetically — these tables are small and a
dropdown that opens with content beats one that opens blank.

`/insurance/refs/airlines` searches **api.airlines** — the airlines the insurance domain itself
knows. That is deliberately NOT the same set as the top-level `/airlines` typeahead, which searches
the much larger `cirium.airlines` matview. Use this one on insurance forms: matching what the write
path will match against is what prevents duplicate airline rows.
"""
from typing import Optional

from fastapi import Request, Response, Depends, Query, status
from sqlalchemy import select, or_

from Config import setup_logger
from settings import Router
from Database.ApiModels import Airlines, AircraftTypes, EngineTypes, Parties
from api_auth import authorize, SCOPE_INSURANCE_READ
from Utils import success_response, error_response
from Utils.ResponsesFunc import build_responses
from Utils.InsuranceCommon import num, party_json, typeahead_order, PARTY_ROLE_FLAGS

logger = setup_logger("insurance_refs_api")

router = Router(prefix="/insurance/refs", tags=["Insurance"])

_DB = "aixii"


def _contains(column, q: str):
    return column.ilike(f"%{q}%")


@router.get(
    "/parties",
    description=(
        "Counterparties for the lessee / lessor / surveyor / leader fields. `role` narrows the list "
        "to entities already seen in that role — leave it off to search every party, since any "
        "party may legitimately be used in any role."
    ),
    responses=build_responses(include={
        status.HTTP_200_OK, status.HTTP_400_BAD_REQUEST, status.HTTP_500_INTERNAL_SERVER_ERROR,
    }),
    dependencies=[Depends(authorize(SCOPE_INSURANCE_READ))],
)
async def search_parties(
    request: Request,
    response: Response,
    q: str = Query("", description="Name substring. Empty returns the first page alphabetically."),
    role: Optional[str] = Query(None, description="lessee | lessor | surveyor | leader"),
    limit: int = Query(20, ge=1, le=100),
):
    try:
        conds = []
        if role:
            flag = PARTY_ROLE_FLAGS.get(role.strip().lower())
            if flag is None:
                return error_response(
                    request=request, response=response,
                    msg=f"Unknown role '{role}'. Allowed: {', '.join(sorted(PARTY_ROLE_FLAGS))}",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            conds.append(getattr(Parties, flag).is_(True))
        q = q.strip()
        if q:
            conds.append(_contains(Parties.name, q))

        stmt = (select(Parties).where(*conds)
                .order_by(*typeahead_order(Parties.name, q)).limit(limit))
        async with request.app.state.db_client.session(_DB) as session:
            rows = (await session.execute(stmt)).scalars().all()
        return success_response(request=request, response=response,
                                data=[party_json(p) for p in rows])
    except Exception as ex:
        logger.error(f"search_parties failed: {ex}")
        return error_response(request=request, response=response, exc=ex)


@router.get(
    "/aircraft-types",
    description=(
        "Aircraft types. The `default_*` fields are the fallbacks the importer uses when a row "
        "carries no technical data — a form can prefill MTOW / engine count from them, but the "
        "aircraft's own specs always win."
    ),
    responses=build_responses(include={
        status.HTTP_200_OK, status.HTTP_500_INTERNAL_SERVER_ERROR,
    }),
    dependencies=[Depends(authorize(SCOPE_INSURANCE_READ))],
)
async def search_aircraft_types(
    request: Request,
    response: Response,
    q: str = Query("", description="Name substring, e.g. 'A320'."),
    limit: int = Query(20, ge=1, le=100),
):
    try:
        q = q.strip()
        conds = [or_(_contains(AircraftTypes.name, q),
                     _contains(AircraftTypes.manufacturer, q))] if q else []
        stmt = (select(AircraftTypes).where(*conds)
                .order_by(*typeahead_order(AircraftTypes.name, q)).limit(limit))
        async with request.app.state.db_client.session(_DB) as session:
            rows = (await session.execute(stmt)).scalars().all()
            data = [
                {
                    "id": t.id,
                    "name": t.name,
                    "manufacturer": t.manufacturer,
                    "default_mtow_kg": num(t.default_mtow_kg),
                    "default_number_of_engines": t.default_number_of_engines,
                    "default_engine_type": (
                        t.default_engine_type.name if t.default_engine_type else None
                    ),
                }
                for t in rows
            ]
        return success_response(request=request, response=response, data=data)
    except Exception as ex:
        logger.error(f"search_aircraft_types failed: {ex}")
        return error_response(request=request, response=response, exc=ex)


@router.get(
    "/engine-types",
    description="Engine models for the `engines_type` field, e.g. 'V2527-A5'.",
    responses=build_responses(include={
        status.HTTP_200_OK, status.HTTP_500_INTERNAL_SERVER_ERROR,
    }),
    dependencies=[Depends(authorize(SCOPE_INSURANCE_READ))],
)
async def search_engine_types(
    request: Request,
    response: Response,
    q: str = Query("", description="Name substring."),
    limit: int = Query(20, ge=1, le=100),
):
    try:
        q = q.strip()
        conds = [or_(_contains(EngineTypes.name, q),
                     _contains(EngineTypes.manufacturer, q))] if q else []
        stmt = (select(EngineTypes).where(*conds)
                .order_by(*typeahead_order(EngineTypes.name, q)).limit(limit))
        async with request.app.state.db_client.session(_DB) as session:
            rows = (await session.execute(stmt)).scalars().all()
        data = [{"id": e.id, "name": e.name, "manufacturer": e.manufacturer} for e in rows]
        return success_response(request=request, response=response, data=data)
    except Exception as ex:
        logger.error(f"search_engine_types failed: {ex}")
        return error_response(request=request, response=response, exc=ex)


@router.get(
    "/airlines",
    description=(
        "Airlines KNOWN TO THE INSURANCE DOMAIN (api.airlines) — match against this on insurance "
        "forms so the write path reuses the existing row instead of creating a near-duplicate. For "
        "the full industry list see the separate /airlines typeahead (cirium.airlines)."
    ),
    responses=build_responses(include={
        status.HTTP_200_OK, status.HTTP_500_INTERNAL_SERVER_ERROR,
    }),
    dependencies=[Depends(authorize(SCOPE_INSURANCE_READ))],
)
async def search_airlines(
    request: Request,
    response: Response,
    q: str = Query("", description="Name substring, or ICAO/IATA prefix."),
    limit: int = Query(20, ge=1, le=100),
):
    try:
        q = q.strip()
        conds = [or_(_contains(Airlines.airline_name, q),
                     Airlines.icao.ilike(f"{q}%"),
                     Airlines.iata.ilike(f"{q}%"))] if q else []
        stmt = (select(Airlines).where(*conds)
                .order_by(*typeahead_order(Airlines.airline_name, q)).limit(limit))
        async with request.app.state.db_client.session(_DB) as session:
            rows = (await session.execute(stmt)).scalars().all()
        data = [{"id": a.id, "name": a.airline_name, "icao": a.icao, "iata": a.iata} for a in rows]
        return success_response(request=request, response=response, data=data)
    except Exception as ex:
        logger.error(f"search_airlines failed: {ex}")
        return error_response(request=request, response=response, exc=ex)
