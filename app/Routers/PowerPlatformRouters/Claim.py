from typing import Annotated, List

from fastapi import status, Request, Query, Body, Response

from Config import setup_logger
from settings import Router
from Schemas import DefaultResponse
from Schemas.PowerPlatform.QuerySchemas.ClaimSchemas import GetClaimQuery, DeleteClaimQuery
from Schemas.PowerPlatform.BodySchemas.DefaultSchemas import CreateClaimBody
from Schemas.PowerPlatform.ClaimSchemas import ClaimSchemaFull, ClaimSchemaLight
from Schemas.Enums import service
from Utils import DBProxy, success_response, error_response, warning_response, cache_key_first_non_null
from Utils.ResponsesFunc import build_responses
from .DBQueries.Claim import query_claims, query_create_claim

logger = setup_logger(name="powerplatform_claims")

router = Router(
    prefix="/powerplatform/claims",
    tags=[service.APITagsEnum.CLAIMS],
)


@router.get(
    path="/full",
    description="Get claims full",
    status_code=status.HTTP_200_OK,
    response_model=DefaultResponse[List[ClaimSchemaFull]],
    responses=build_responses(
        list[ClaimSchemaFull],
        success_status=status.HTTP_200_OK,
        include={status.HTTP_200_OK, status.HTTP_404_NOT_FOUND, status.HTTP_500_INTERNAL_SERVER_ERROR}
    )
)
async def get_claims_full(request: Request, response: Response, _payload: Annotated[GetClaimQuery, Query()]):
    db_proxy: DBProxy = request.state.db_proxy

    async def db_query(session):
        return await query_claims(session, _payload=_payload, full=True)

    try:
        cache_key = cache_key_first_non_null(name="claim:full", data=_payload.model_dump(),
                                             keys=("user_id", "claim_id"),
                                             fallback="all")

        airline_data = await db_proxy.get_or_cache(
            key=cache_key,
            db_name="powerplatform",
            query_func=db_query,
            ttl=30
        )

        if len(airline_data) > 0:
            return success_response(request=request, response=response, data=airline_data,
                                    msg="Claim(-s) retrieved successfully")
        return warning_response(request=request, response=response, msg="Claim(-s) not found",
                                status_code=status.HTTP_404_NOT_FOUND)

    except ValueError:
        return warning_response(request=request, response=response, msg="Claim(-s) not found",
                                status_code=status.HTTP_404_NOT_FOUND)
    except Exception as _ex:
        logger.error(f"Failed to get Claim(-s): {_ex}")
        return error_response(request=request, response=response, exc=_ex)\


@router.get(
    path="/full",
    description="Get claims light",
    status_code=status.HTTP_200_OK,
    response_model=DefaultResponse[List[ClaimSchemaLight]],
    responses=build_responses(
        list[ClaimSchemaLight],
        success_status=status.HTTP_200_OK,
        include={status.HTTP_200_OK, status.HTTP_404_NOT_FOUND, status.HTTP_500_INTERNAL_SERVER_ERROR}
    )
)
async def get_claims_light(request: Request, response: Response, _payload: Annotated[GetClaimQuery, Query()]):
    db_proxy: DBProxy = request.state.db_proxy

    async def db_query(session):
        return await query_claims(session, _payload=_payload, full=False)

    try:
        cache_key = cache_key_first_non_null(name="claim:light", data=_payload.model_dump(),
                                             keys=("user_id", "claim_id"),
                                             fallback="all")

        airline_data = await db_proxy.get_or_cache(
            key=cache_key,
            db_name="powerplatform",
            query_func=db_query,
            ttl=30
        )

        if len(airline_data) > 0:
            return success_response(request=request, response=response, data=airline_data,
                                    msg="Claim(-s) retrieved successfully")
        return warning_response(request=request, response=response, msg="Claim(-s) not found",
                                status_code=status.HTTP_404_NOT_FOUND)

    except ValueError:
        return warning_response(request=request, response=response, msg="Claim(-s) not found",
                                status_code=status.HTTP_404_NOT_FOUND)
    except Exception as _ex:
        logger.error(f"Failed to get Claim(-s): {_ex}")
        return error_response(request=request, response=response, exc=_ex)


@router.post(
    path="",
    description="Create claim",
    status_code=status.HTTP_201_CREATED,
    response_model=DefaultResponse[List[]],
    responses=build_responses(
        list[],
        success_status=status.HTTP_201_CREATED,
        include={status.HTTP_201_CREATED, status.HTTP_404_NOT_FOUND, status.HTTP_500_INTERNAL_SERVER_ERROR}
    )
)
async def create_claim(request: Request, _payload: Annotated[CreateClaimBody, Body()]):
    db_proxy: DBProxy = request.state.db_proxy

    try:
        async def db_query(session):
            return await query_create_claim(session, _payload)

        cache_key = cache_key_first_non_null(name="create_claims", data=_payload.model_dump(),
                                             keys=("claim_id", "user_id"),
                                             fallback="all")


        result = await db_proxy.update_and_cache(
            key=cache_key,
            db_name="powerplatform",
            update_func=db_query,
            ttl=1
        )
        return success_response(request=request, msg=f"Claim {result} successfully")

    except Exception as _ex:
        logger.error(f"Failed to create/update Claim: {_ex}")
        return error_response(request=request, exc=_ex)


@router.delete(
    path="",
    description="Delete claim",
    status_code=status.HTTP_200_OK,
    response_model=DefaultResponse,
)
async def delete_claim(request: Request, _payload: Annotated[ClaimsDeleteQuery, Query()]):
    db_proxy: DBProxy = request.state.db_proxy
    try:
        async def db_query(session):
            return await delete_claim_query(session, _payload)

        cache_key = "claim:delete"

        result = await db_proxy.update_and_cache(
            key=cache_key,
            db_name="powerplatform",
            update_func=db_query,
            ttl=1
        )
        if result:
            return success_response(request=request, msg=f"Claim '#{_payload.claim_id}' deleted successfully")

    except Exception as _ex:
        logger.error(f"Failed to delete claim: {_ex}")
        return error_response(request=request, exc=_ex)


@router.post(
    path="/sumpolicy",
    description="Sum policy",
    status_code=status.HTTP_200_OK,
    response_model=DefaultResponse,
)
async def sum_policy(request: Request, _payload: Annotated[SumPolicyBodySchema, Body()]):
    db_proxy: DBProxy = request.state.db_proxy

    try:
        async def db_query(session):
            return await query_sum_policy(session, _payload)

        cache_key = f"sumpolicy:{_payload.aircraft_id}"

        result = await db_proxy.get_or_cache(
            key=cache_key,
            db_name="powerplatform",
            query_func=db_query,
            ttl=10
        )
        return success_response(request=request, data=result, msg=f"Sum policy retrieved successfully")

    except Exception as _ex:
        logger.error(f"Failed to sum policy: {_ex}")
        return error_response(request=request, exc=_ex)



