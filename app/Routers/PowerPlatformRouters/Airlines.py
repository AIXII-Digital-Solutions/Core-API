from typing import Annotated, List

from fastapi import status, Request, Query, Body, Response

from Config import setup_logger
from settings import Router
from Schemas import DefaultResponse, AirlineUsersSchemaFull, AirlineUsersSchemaLight, UpsertdelResponseSchema
from Schemas.PowerPlatform.BodySchemas.AirlineSchemas import CreateAirlinesBody
from Schemas.PowerPlatform.QuerySchemas.AirlineSchemas import GetAirlineQuery
from Schemas.Enums import service
from Utils import DBProxy, success_response, error_response, warning_response, cache_key_first_non_null
from Utils.ResponsesFunc import build_responses
from .DBQueries.Airlines import query_airline, query_create_airline

logger = setup_logger(name="powerplatform_airlines")

router = Router(
    prefix="/powerplatform/airlines",
    tags=[service.APITagsEnum.AIRLINES],
)


@router.get(
    path="/airlines/full",
    description="Get Airlines",
    status_code=status.HTTP_200_OK,
    response_model=DefaultResponse[List[AirlineUsersSchemaFull]],
    responses=build_responses(
        include={status.HTTP_200_OK, status.HTTP_404_NOT_FOUND, status.HTTP_500_INTERNAL_SERVER_ERROR}
    )
)
async def get_airlines_full(request: Request, response: Response, _payload: Annotated[GetAirlineQuery, Query()]):
    db_proxy: DBProxy = request.state.db_proxy

    async def db_query(session):
        return await query_airline(session, _payload=_payload, full=True)

    try:
        cache_key = cache_key_first_non_null(name="airline:full", data=_payload.model_dump(),
                                             keys=("user_id", "airline_name", "airline_id"),
                                             fallback="all")
        airline_data = await db_proxy.get_or_cache(
            key=cache_key,
            db_name="powerplatform",
            query_func=db_query,
            ttl=60
        )
        if len(airline_data) > 0:
            return success_response(request=request, response=response, data=airline_data, msg="Airline(-s) retrieved successfully")
        return warning_response(request=request, response=response, msg="Airline(-s) not found",
                                status_code=status.HTTP_404_NOT_FOUND)

    except ValueError:
        return warning_response(request=request, response=response, msg="Airline(-s) not found", status_code=status.HTTP_404_NOT_FOUND)
    except Exception as _ex:
        logger.error(f"Failed to get Airline(-s): {_ex}")
        return error_response(request=request, response=response, exc=_ex)


@router.get(
    path="/airlines/light",
    description="Get Airlines",
    status_code=status.HTTP_200_OK,
    response_model=DefaultResponse[List[AirlineUsersSchemaLight]],
    responses=build_responses(
        include={status.HTTP_200_OK, status.HTTP_404_NOT_FOUND, status.HTTP_500_INTERNAL_SERVER_ERROR}
    )
)
async def get_airlines_light(request: Request, response: Response, _payload: Annotated[GetAirlineQuery, Query()]):
    db_proxy: DBProxy = request.state.db_proxy

    async def db_query(session):
        return await query_airline(session, _payload=_payload, full=False)

    try:
        cache_key = cache_key_first_non_null(name="airline:light", data=_payload.model_dump(),
                                             keys=("user_id", "airline_name", "airline_id"),
                                             fallback="all")
        airline_data = await db_proxy.get_or_cache(
            key=cache_key,
            db_name="powerplatform",
            query_func=db_query,
            ttl=60
        )
        if len(airline_data) > 0:
            return success_response(request=request, response=response, data=airline_data, msg="Airline(-s) retrieved successfully")
        return warning_response(request=request, response=response, msg="Airline(-s) not found", status_code=status.HTTP_404_NOT_FOUND)

    except ValueError:
        return warning_response(request=request, response=response, msg="Airline(-s) not found", status_code=status.HTTP_404_NOT_FOUND)
    except Exception as _ex:
        logger.error(f"Failed to get Airline(-s): {_ex}")
        return error_response(request=request, response=response, exc=_ex)


@router.post(
    path="/airlines",
    description="Create Airlines",
    status_code=status.HTTP_201_CREATED,
    response_model=DefaultResponse[List[UpsertdelResponseSchema]],
    responses=build_responses(
        include={status.HTTP_201_CREATED, status.HTTP_500_INTERNAL_SERVER_ERROR}
    )
)
async def create_airlines(request: Request, response: Response, _payload: Annotated[CreateAirlinesBody, Body()]):
    db_proxy: DBProxy = request.state.db_proxy

    async def db_query(session):
        return await query_create_airline(session, _payload=_payload)

    try:
        cache_key = f"airline:{_payload.airline_icao}"
        result = await db_proxy.update_and_cache(
            key=cache_key,
            db_name="powerplatform",
            update_func=db_query,
            ttl=60
        )

        return success_response(request=request, response=response, msg="Airline created successfully",
                                status_code=status.HTTP_201_CREATED, data=result)

    except Exception as _ex:
        logger.error(f"Failed to create airline: {_ex}")
        return error_response(request=request, response=response, exc=_ex)




