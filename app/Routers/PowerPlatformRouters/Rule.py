from typing import List
from uuid import UUID

from fastapi import status, Request, Response

from Config import setup_logger
from settings import Router
from Utils.ResponsesFunc import build_responses
from .DBQueries.Rule import query_rules
from Schemas import DefaultResponse, ApplicationRulesSchema
from Schemas.Enums import service
from Utils import DBProxy, success_response, warning_response, error_response

logger = setup_logger(name="powerplatform_rules")

router = Router(
    prefix="/powerplatform/rules",
    tags=[service.APITagsEnum.RULES],
)

@router.get(
    path="/",
    description="Get all rules",
    status_code=status.HTTP_200_OK,
    response_model=DefaultResponse[List[ApplicationRulesSchema]],
    responses=build_responses(
        include={status.HTTP_200_OK, status.HTTP_404_NOT_FOUND, status.HTTP_500_INTERNAL_SERVER_ERROR}
    )
)
async def get_rules(request: Request, response: Response):
    db_proxy: DBProxy = request.state.db_proxy

    async def db_query(session):
        return await query_rules(session)

    try:
        cache_key = "rules:all"
        rules_data = await db_proxy.get_or_cache(
            key=cache_key,
            db_name="powerplatform",
            query_func=db_query,
            ttl=60
        )

        if len(rules_data) > 0:
            return success_response(request=request, response=response, data=rules_data, msg="Rules retrieved successfully")
        return warning_response(request=request, response=response, msg="Rules not found", status_code=status.HTTP_404_NOT_FOUND)
    except Exception as _ex:
        return error_response(request=request, response=response, exc=_ex)


@router.get(
    path="/{application_id}",
    description="Get application rules",
    status_code=status.HTTP_200_OK,
    response_model=DefaultResponse[List[ApplicationRulesSchema]],
    responses=build_responses(
        include={status.HTTP_200_OK, status.HTTP_404_NOT_FOUND, status.HTTP_500_INTERNAL_SERVER_ERROR}
    )
)
async def get_rules(request: Request, response: Response, application_id: UUID):
    db_proxy: DBProxy = request.state.db_proxy

    async def db_query(session):
        return await query_rules(session)

    try:
        cache_key = f"rules:{application_id}"
        rules_data = await db_proxy.get_or_cache(
            key=cache_key,
            db_name="powerplatform",
            query_func=db_query,
            ttl=60
        )

        if len(rules_data) > 0:
            return success_response(request=request, response=response, data=rules_data, msg="Rules retrieved successfully")
        return warning_response(request=request, response=response, msg="Rules not found", status_code=status.HTTP_404_NOT_FOUND)
    except Exception as _ex:
        logger.error(f"Failed to get rules: {_ex}")
        return error_response(request=request, response=response, exc=_ex)