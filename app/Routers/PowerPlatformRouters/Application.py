from datetime import datetime
from typing import Annotated, List

from fastapi import status, Request, Query, Response

from Config import setup_logger
from settings import Router
from Schemas import DefaultResponse, ApplicationSchema, FontSchema, ApplicationAppearanceSchema, UpsertdelResponseSchema
from Schemas.Enums import service
from Schemas.PowerPlatform.QuerySchemas.ApplicationSchemas import GetApplicationIdQuery, DeviceInfo, \
    UpsertAppearanceQuery
from Utils import DBProxy, success_response, warning_response, error_response, cache_key_first_non_null
from Utils.ResponsesFunc import build_responses
from .DBQueries.Application import query_fonts, query_apps, query_get_appearance, query_upsert_appearance

logger = setup_logger(name="powerplatform_application")

router = Router(
    prefix="/powerplatform/application",
    tags=[service.APITagsEnum.APPLICATIONS],
)


@router.get(
    path="/",
    description="Get applications info",
    status_code=status.HTTP_200_OK,
    response_model=DefaultResponse[List[ApplicationSchema]],
    responses=build_responses(
        include={status.HTTP_200_OK, status.HTTP_404_NOT_FOUND, status.HTTP_500_INTERNAL_SERVER_ERROR}
    )
)
async def get_apps(request: Request, response: Response, _payload: Annotated[GetApplicationIdQuery, Query()]):
    db_proxy: DBProxy = request.state.db_proxy

    async def db_query(session):
        return await query_apps(session, _payload)

    try:
        cache_key = cache_key_first_non_null(name="application", data=_payload.model_dump(),
                                             keys=(
                                                 "application_id"),
                                             fallback="all")

        apps_data = await db_proxy.get_or_cache(
            key=cache_key,
            db_name="powerplatform",
            query_func=db_query,
            ttl=600
        )

        if len(apps_data) > 0:
            return success_response(request=request, response=response, data=apps_data,
                                    msg="Application(-s) retrieved successfully")
        return warning_response(request=request, response=response, msg="Application(-s) not found",
                                status_code=status.HTTP_404_NOT_FOUND)
    except Exception as _ex:
        logger.error(f"Failed to get Application(-s): {_ex}")
        return error_response(request=request, response=response, exc=_ex)


@router.get(
    path="/fonts",
    description="Get all fonts by screen size",
    status_code=status.HTTP_200_OK,
    response_model=DefaultResponse[List[FontSchema]],
    responses=build_responses(
        include={status.HTTP_200_OK, status.HTTP_404_NOT_FOUND, status.HTTP_500_INTERNAL_SERVER_ERROR}
    )
)
async def get_fonts(request: Request, response: Response, _payload: Annotated[DeviceInfo, Query()]):
    db_proxy: DBProxy = request.state.db_proxy

    async def db_query(session):
        return await query_fonts(session, _payload)

    try:
        cache_key = f"fonts:{_payload.screen_size}"
        fonts_data = await db_proxy.get_or_cache(
            key=cache_key,
            db_name="powerplatform",
            query_func=db_query,
            ttl=2
        )

        if len(fonts_data) > 0:
            return success_response(request=request, response=response, data=fonts_data,
                                    msg="Fonts retrieved successfully")
        return warning_response(request=request, response=response, msg="Fonts not found",
                                status_code=status.HTTP_404_NOT_FOUND)
    except Exception as _ex:
        logger.error(f"Failed to get fonts: {_ex}")
        return error_response(request=request, response=response, exc=_ex)


@router.get(
    path="/appearance",
    description="Get appearance by user device",
    status_code=status.HTTP_200_OK,
    response_model=DefaultResponse[List[ApplicationAppearanceSchema]],
    responses=build_responses(
        include={status.HTTP_200_OK, status.HTTP_404_NOT_FOUND, status.HTTP_500_INTERNAL_SERVER_ERROR}
    )
)
async def get_appearance(request: Request, response: Response, _payload: Annotated[DeviceInfo, Query()]):
    db_proxy: DBProxy = request.state.db_proxy

    async def db_query(session):
        return await query_get_appearance(session, _payload)

    try:
        cache_key = f"appearance:{_payload.user_id}:{_payload.os_type}"
        appearance_data = await db_proxy.get_or_cache(
            key=cache_key,
            db_name="powerplatform",
            query_func=db_query,
            ttl=1
        )

        if len(appearance_data) > 0:
            return success_response(request=request, response=response, data=appearance_data,
                                    msg="Appearance retrieved successfully")
        return warning_response(request=request, response=response, msg="Appearance not found",
                                status_code=status.HTTP_404_NOT_FOUND)
    except Exception as _ex:
        logger.error(f"Failed to get appearance: {_ex}")
        return error_response(request=request, response=response, exc=_ex)


@router.post(
    path="/appearance",
    description="Create or update appearance",
    status_code=status.HTTP_201_CREATED,
    response_model=DefaultResponse[List[UpsertdelResponseSchema]],
    responses=build_responses(
        include={status.HTTP_201_CREATED, status.HTTP_500_INTERNAL_SERVER_ERROR}
    )
)
async def create_update_appearance(request: Request, response: Response, _payload: Annotated[UpsertAppearanceQuery, Query()]):
    db_proxy: DBProxy = request.state.db_proxy

    async def db_query(session):
        return await query_upsert_appearance(session, _payload)

    try:
        cache_key = f"appearance:{datetime.now()}"
        appearance_data = await db_proxy.get_or_cache(
            key=cache_key,
            db_name="powerplatform",
            query_func=db_query,
            ttl=1
        )

        return success_response(request=request, response=response, data=appearance_data,
                                    msg="Appearance created/updated successfully", status_code=status.HTTP_201_CREATED)
    except Exception as _ex:
        logger.error(f"Failed to get appearance: {_ex}")
        return error_response(request=request, response=response, exc=_ex)

