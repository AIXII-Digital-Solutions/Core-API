from typing import Annotated, List
from uuid import UUID

from fastapi import Request, status, Query, Response

from Config import setup_logger
from settings import Router
from Schemas import DefaultResponse, UserSchemaFull, UserSchemaLight, UserAccessSchema, UpsertdelResponseSchema
from Schemas.PowerPlatform.QuerySchemas.ApplicationSchemas import GetApplicationIdQuery
from Schemas.Enums import service
from Schemas.PowerPlatform.QuerySchemas.DefaultSchemas import SwitchUserAppearanceQuery
from Utils import DBProxy, success_response, warning_response, error_response
from Utils.ResponsesFunc import build_responses
from .DBQueries.User import query_users, query_user_access, query_switch_user_appearance

logger = setup_logger(name="powerplatform_users")

router = Router(
    prefix="/powerplatform/users",
    tags=[service.APITagsEnum.USERS],
)


@router.get(
    path="/full",
    description="Get users information",
    status_code=status.HTTP_200_OK,
    response_model=DefaultResponse[List[UserSchemaFull]],
    responses=build_responses(
        include={status.HTTP_200_OK, status.HTTP_404_NOT_FOUND, status.HTTP_500_INTERNAL_SERVER_ERROR}
    )
)
async def users(request: Request, response: Response):
    db_proxy: DBProxy = request.state.db_proxy

    async def db_query(session):
        return await query_users(session, full=True)

    try:
        cache_key = "users:full:all"
        users_data = await db_proxy.get_or_cache(
            key=cache_key,
            db_name="powerplatform",
            query_func=db_query,
            ttl=60
        )

        if len(users_data) > 0:
            return success_response(request=request, response=response, data=users_data, msg="Users retrieved successfully")
        return warning_response(request=request, response=response, msg="Users not found", status_code=status.HTTP_404_NOT_FOUND)

    except Exception as _ex:
        return error_response(request=request, response=response, exc=_ex)


@router.get(
    path="/light",
    description="Get users information",
    status_code=status.HTTP_200_OK,
    response_model=DefaultResponse[List[UserSchemaLight]],
    responses=build_responses(
        include={status.HTTP_200_OK, status.HTTP_404_NOT_FOUND, status.HTTP_500_INTERNAL_SERVER_ERROR}
    )
)
async def users(request: Request, response: Response):
    db_proxy: DBProxy = request.state.db_proxy

    async def db_query(session):
        return await query_users(session, full=False)

    try:
        cache_key = "users:light:all"
        users_data = await db_proxy.get_or_cache(
            key=cache_key,
            db_name="powerplatform",
            query_func=db_query,
            ttl=60
        )

        if len(users_data) > 0:
            return success_response(request=request, response=response, data=users_data, msg="Users retrieved successfully")
        return warning_response(request=request, response=response, msg="Users not found", status_code=status.HTTP_404_NOT_FOUND)

    except Exception as _ex:
        return error_response(request=request, response=response, exc=_ex)


@router.get(
    path="/{user_id}/full",
    description="Get user information",
    status_code=status.HTTP_200_OK,
    response_model=DefaultResponse[List[UserSchemaFull]],
    responses=build_responses(
        include={status.HTTP_200_OK, status.HTTP_404_NOT_FOUND, status.HTTP_500_INTERNAL_SERVER_ERROR}
    )
)
async def users(request: Request, response: Response, user_id: UUID):
    db_proxy: DBProxy = request.state.db_proxy

    async def db_query(session):
        return await query_users(session, full=True, user_id=user_id)

    try:
        cache_key = f"users:full:{user_id}"
        user_data = await db_proxy.get_or_cache(
            key=cache_key,
            db_name="powerplatform",
            query_func=db_query,
            ttl=30
        )

        if len(user_data) > 0:
            return success_response(request=request, response=response, data=user_data, msg="User retrieved successfully")
        return warning_response(request=request, response=response, msg="User not found", status_code=status.HTTP_404_NOT_FOUND)
    except Exception as _ex:
        return error_response(request=request, response=response, exc=_ex)


@router.get(
    path="/{user_id}/light",
    description="Get user information",
    status_code=status.HTTP_200_OK,
    response_model=DefaultResponse[List[UserSchemaLight]],
    responses=build_responses(
        include={status.HTTP_200_OK, status.HTTP_404_NOT_FOUND, status.HTTP_500_INTERNAL_SERVER_ERROR}
    )
)
async def users(request: Request, response: Response, user_id: UUID):
    db_proxy: DBProxy = request.state.db_proxy

    async def db_query(session):
        return await query_users(session, full=False, user_id=user_id)

    try:
        cache_key = f"users:light:{user_id}"
        user_data = await db_proxy.get_or_cache(
            key=cache_key,
            db_name="powerplatform",
            query_func=db_query,
            ttl=30
        )

        if len(user_data) > 0:
            return success_response(request=request, response=response, data=user_data, msg="User retrieved successfully")
        return warning_response(request=request, response=response, msg="User not found", status_code=status.HTTP_404_NOT_FOUND)
    except Exception as _ex:
        return error_response(request=request, response=response, exc=_ex)


@router.get(
    path="/{user_id}/access",
    description="Get user's access rules",
    status_code=status.HTTP_200_OK,
    response_model=DefaultResponse[List[UserAccessSchema]],
    responses=build_responses(
        include={status.HTTP_200_OK, status.HTTP_404_NOT_FOUND, status.HTTP_500_INTERNAL_SERVER_ERROR}
    )
)
async def users_access(request: Request, response: Response, user_id: UUID, _payload: Annotated[GetApplicationIdQuery, Query()]):
    db_proxy: DBProxy = request.state.db_proxy

    async def db_query(session):
        return await query_user_access(session, user_id, _payload=_payload)

    try:
        if _payload.application_id is None:
            cache_key = f"user_access:{user_id}"
        else:
            cache_key = f"user_access:{user_id}|{_payload.application_id}"
        user_data = await db_proxy.get_or_cache(
            key=cache_key,
            db_name="powerplatform",
            query_func=db_query,
            ttl=120
        )

        if len(user_data) > 0:
            return success_response(request=request, response=response, data=user_data, msg="User's rules retrieved successfully")
        return warning_response(request=request, response=response, msg="User not found",
                                status_code=status.HTTP_404_NOT_FOUND)
    except Exception as _ex:
        logger.error(f"Failed to get user's access rules: {_ex}")
        return error_response(request=request, response=response, exc=_ex)


@router.post(
    path="/switchappearance",
    description="Switch user appearance",
    status_code=status.HTTP_200_OK,
    response_model=DefaultResponse[List[UpsertdelResponseSchema]],
    responses=build_responses(
        include={status.HTTP_200_OK, status.HTTP_500_INTERNAL_SERVER_ERROR}
    )
)
async def switch_user_appearance(request: Request, response: Response, _payload: Annotated[SwitchUserAppearanceQuery, Query()]):
    db_proxy: DBProxy = request.state.db_proxy

    async def db_query(session):
        return await query_switch_user_appearance(session, _payload)

    try:
        cache_key = f"users:appearance:{_payload.user_id}:{_payload.os_type}"
        data = await db_proxy.get_or_cache(
            key=cache_key,
            db_name="powerplatform",
            query_func=db_query,
            ttl=2
        )

        if len(data) > 0:
            return success_response(request=request, response=response, data=data, msg="User appearance switched successfully")
        return warning_response(request=request, response=response, msg="User not found", status_code=status.HTTP_404_NOT_FOUND)
    except Exception as _ex:
        return error_response(request=request, response=response, exc=_ex)
