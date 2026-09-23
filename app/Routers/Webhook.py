import hmac

from fastapi import Request, status, Response
from fastapi.responses import PlainTextResponse

from Config import setup_logger
from settings import MS_WEBHOOK_SECRET, Router
from Queue import EXTERNAL_QUEUE
from Schemas import DefaultResponse
from Schemas.Enums import service
from Utils import success_response, warning_response

logger = setup_logger(name="webhooks")

router = Router(
    prefix="/webhooks",
    tags=[service.APITagsEnum.WEBHOOK],
)

MSGraphWebhookResponses = {
    200: {"model": DefaultResponse, "description": "Success"},
    510: {"model": DefaultResponse, "description": "Client state not authorized"},
    500: {"model": DefaultResponse, "description": "Server error"},
}

# TODO: Update this router

def _client_state_ok(value: dict) -> bool:
    """The webhook's ONLY credential, compared the way a credential has to be.

    `!=` on a str short-circuits at the first differing byte, which leaks the length of the shared
    secret and a little of its content to anyone who can time the reply — every other credential in
    this service already uses `compare_digest` (see service_auth.py and api_auth.py) and this one
    did not.
    """
    sent = value.get("clientState")
    if not isinstance(sent, str) or not MS_WEBHOOK_SECRET:
        return False
    return hmac.compare_digest(sent.encode("utf-8"), MS_WEBHOOK_SECRET.encode("utf-8"))


def _unauthorized(request, response):
    # The rejected value is NOT echoed back. Repeating a failed secret to whoever sent it turns
    # the endpoint into an oracle, and the value is in the log line either way.
    return warning_response(request=request, msg="clientState not authorized",
                            status_code=status.HTTP_510_NOT_EXTENDED, response=response)


def _notifications(data: dict) -> list:
    """Graph's notification array. An empty or absent one is REJECTED rather than iterated over:
    the secret check lived inside the loop, so a body of `{"value": []}` ran zero iterations,
    validated nothing, fell off the end of the handler and returned a bare `200 null` — outside
    the envelope every other route guarantees."""
    items = data.get("value")
    return items if isinstance(items, list) and items else []


@router.post("/microsoft",
             status_code=status.HTTP_200_OK,
             responses=MSGraphWebhookResponses,
             summary="MicrosoftGraph",
             description="Receive webhook from Microsoft Graph",
             )
async def microsoft(request: Request, response: Response):
    logger.info("[MicrosoftGraph] Webhook received")
    data = await request.json()

    # validationToken check
    validation_token = data.get("validationToken")
    if validation_token:
        logger.info("[MicrosoftGraph] Validation token received, responding with plain text")
        # Graph accepts a subscription only if the body is the RAW token as text/plain. The log
        # line has always said "plain text"; the code wrapped it in the JSON envelope instead, so
        # creating or renewing a subscription against this endpoint could not succeed.
        return PlainTextResponse(validation_token, status_code=status.HTTP_200_OK)

    # clientState check
    notifications = _notifications(data)
    if not notifications:
        return _unauthorized(request, response)
    for value in notifications:
        if not _client_state_ok(value):
            logger.warning("[MicrosoftGraph] Webhook clientState not recognised: %r",
                           value.get("clientState"))
            return _unauthorized(request, response)

    for event in notifications:
        user_id = event["resourceData"]["id"]
    #     TODO: Final this shit
    # Explicitly, and inside the envelope: the handler used to fall off the end and return a bare
    # `200 null`, which is not the shape any client of this API is written against.
    return success_response(request=request, response=response, data=[None],
                            msg="Notification accepted", status_code=status.HTTP_202_ACCEPTED)


MSGraphWebhookResponses.pop(200)
MSGraphWebhookResponses[202] = {"model": DefaultResponse, "description": "Accepted"}


@router.post("/microsoft/lifecycle",
             status_code=status.HTTP_202_ACCEPTED,
             responses=MSGraphWebhookResponses,
             summary="MicrosoftGraph lifecycle",
             description="Receive lifecycle webhook from Microsoft Graph",
             )
async def microsoft_lifecycle(request: Request, response: Response):
    logger.info("[MicrosoftGraph] Lifecycle webhook received")
    data = await request.json()

    # validationToken check
    validation_token = data.get("validationToken")
    if validation_token:
        logger.info("[MicrosoftGraph] Validation token received, responding with plain text")
        return PlainTextResponse(validation_token, status_code=status.HTTP_200_OK)

    # clientState check

    notifications = _notifications(data)
    if not notifications:
        return _unauthorized(request, response)
    for value in notifications:
        if not _client_state_ok(value):
            logger.warning("[MicrosoftGraph] Lifecycle clientState not recognised: %r",
                           value.get("clientState"))
            return _unauthorized(request, response)

        if value.get("lifecycleEvent") == "reauthorizationRequired":
            subscription_id = value.get("subscriptionId")
            await request.state.arq.enqueue_job(
                "refresh_subscription",
                subscription_id=subscription_id,
                _queue_name=EXTERNAL_QUEUE,
            )

            # `data=validation_token` was always None here: this branch only runs when the body
            # carried no validationToken, which is what the early return above handles.
            return success_response(request=request, response=response, data=[None],
                                    status_code=status.HTTP_202_ACCEPTED,
                                    msg="Subscription reauthorized")

    # A lifecycle body carrying no reauthorizationRequired is legitimate (Graph also sends
    # subscriptionRemoved and missed). Answer inside the envelope rather than falling off the end
    # of the function and returning a bare `200 null`.
    return success_response(request=request, response=response, data=[None],
                            status_code=status.HTTP_202_ACCEPTED,
                            msg="Lifecycle notification accepted")
