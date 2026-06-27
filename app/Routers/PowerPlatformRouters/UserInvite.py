from typing import Annotated, List

from fastapi import Request, status, Query, Response

from Config import setup_logger
from settings import Router
from Queue import EXTERNAL_QUEUE
from Schemas import InviteUserSchema, InviteUserSchemaQuery, DefaultResponse
from Schemas.Enums import service
from Utils import success_response, warning_response, error_response
from Utils.ResponsesFunc import build_responses

logger = setup_logger(name="msgraph_invite_user")

router = Router(
    prefix="/msgraph",
    tags=[service.APITagsEnum.USERS],
)


@router.post(
    "/invite_user",
    description="Create user invitation process by user data",
    status_code=status.HTTP_201_CREATED,
    response_model=DefaultResponse[List[None]],
    responses=build_responses(
        include={status.HTTP_201_CREATED, status.HTTP_400_BAD_REQUEST, status.HTTP_500_INTERNAL_SERVER_ERROR}
    )
)
async def invite_user(request: Request, response: Response, user_data: Annotated[InviteUserSchemaQuery, Query()]):
    valid_data = InviteUserSchema(
        **user_data.model_dump()
    )
    try:
        # The MS Graph client lives in external_worker; enqueue the invite and
        # wait for its result so the API can still return a synchronous response.
        job = await request.state.arq.enqueue_job(
            "invite_guest",
            data=valid_data.model_dump(),
            _queue_name=EXTERNAL_QUEUE,
        )
        result = await job.result(timeout=30)
        if result is not None:
            return success_response(request=request, response=response, msg="Invitation created",
                                    status_code=status.HTTP_201_CREATED, data=[])
        return warning_response(request=request, msg="Invitation was not created", response=response,
                                status_code=status.HTTP_400_BAD_REQUEST)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)
