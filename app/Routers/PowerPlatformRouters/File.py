from typing import Annotated, List

from fastapi import status, Request, Query, Body, Response

from Config import setup_logger
from settings import Router
from Schemas import DefaultResponse, ApplicationFileLoadBody, AssetSchema, UpsertdelResponseSchema
from Schemas.PowerPlatform.QuerySchemas.ApplicationSchemas import GetApplicationFileQuery
from Schemas.Enums import service
from Utils import DBProxy, success_response, error_response, warning_response
from Utils.ResponsesFunc import build_responses
from .DBQueries.File import query_load_file, query_file

logger = setup_logger(name="powerplatform_files")

router = Router(
    prefix="/powerplatform/files",
    tags=[service.APITagsEnum.FILES],
)


@router.post(
    path="/",
    description="Upload file to Database",
    status_code=status.HTTP_201_CREATED,
    response_model=DefaultResponse[List[UpsertdelResponseSchema]],
    responses=build_responses(
        include={status.HTTP_201_CREATED, status.HTTP_500_INTERNAL_SERVER_ERROR}
    )
)
async def upload_file(request: Request, response: Response, _payload: Annotated[ApplicationFileLoadBody, Body()]):
    db_proxy: DBProxy = request.state.db_proxy

    async def db_query(session):
        return await query_load_file(session, _payload=_payload)

    try:
        cache_key = f"file:{_payload.file_name}"
        result = await db_proxy.update_and_cache(
            key=cache_key,
            db_name="powerplatform",
            update_func=db_query,
            ttl=600
        )

        return success_response(request=request, response=response, data=result, msg="File uploaded successfully", status_code=status.HTTP_201_CREATED)

    except Exception as _ex:
        logger.error(f"Failed to upload file: {_ex}")
        return error_response(request=request, response=response, exc=_ex)


@router.get(
    path="/",
    description="Get file from Database",
    status_code=status.HTTP_200_OK,
    response_model=DefaultResponse[List[AssetSchema]],
    responses=build_responses(
        include={status.HTTP_200_OK, status.HTTP_404_NOT_FOUND, status.HTTP_500_INTERNAL_SERVER_ERROR}
    )
)
async def get_file(request: Request, response: Response, _payload: Annotated[GetApplicationFileQuery, Query()]):
    db_proxy: DBProxy = request.state.db_proxy

    async def db_query(session):
        return await query_file(session, _payload=_payload)

    try:
        if _payload.file_name:
            cache_key = f"file:{_payload.file_name}"
        else:
            cache_key = f"file:{_payload.file_id}"
        file_data = await db_proxy.get_or_cache(
            key=cache_key,
            db_name="powerplatform",
            query_func=db_query,
            ttl=600
        )
        if len(file_data) > 0:
            return success_response(request=request, response=response, data=file_data,
                                    msg="File retrieved successfully")
        return warning_response(request=request, response=response, msg="File not found",
                                status_code=status.HTTP_404_NOT_FOUND)
    except ValueError as _vex:
        return warning_response(request=request, response=response, msg="File not found", status_code=status.HTTP_404_NOT_FOUND)
    except Exception as _ex:
        logger.error(f"Failed to get file: {_ex}")
        return error_response(request=request, response=response, exc=_ex)
