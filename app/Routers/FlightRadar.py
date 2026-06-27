from pathlib import Path
import random
from typing import Annotated, List

from fastapi import Request, status, Query, BackgroundTasks, Response
from fastapi.responses import FileResponse

from Config import setup_logger
from settings import Router, RESPONSES_PATH
from Queue import EXTERNAL_QUEUE
from Schemas import RequestFRFlightSummary, RequestFRAirports, DefaultResponse
from Schemas.Enums import service
from Utils import success_response, error_response, str_to_list
from Utils.ResponsesFunc import build_responses

logger = setup_logger(name="flightradar_api")


router = Router(
    prefix="/flightradar",
    tags=[service.APITagsEnum.FLIGHTRADAR],
)


@router.get(
    path="/flightsummary",
    description="Start Flight Summary process",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DefaultResponse[List[None]],
    responses=build_responses(
        include={status.HTTP_202_ACCEPTED, status.HTTP_404_NOT_FOUND, status.HTTP_500_INTERNAL_SERVER_ERROR}
    )
)
async def process_data(request: Request, response: Response,
                       background_tasks: BackgroundTasks,
                       _payload: Annotated[RequestFRFlightSummary, Query()]):
    payload = RequestFRFlightSummary(
        **_payload.model_dump()
    )

    try:
        start_date_str, end_date_str = payload.start_date.strftime("%Y-%m-%d"), payload.end_date.strftime("%Y-%m-%d")
        regs, airlines = payload.regs.split(", "), payload.airlines.split(", ")
        if len(airlines) > 0 and  airlines[0] != "":
            regs = None
        else:
            airlines = None

        await request.state.arq.enqueue_job(
            "fetch_flight_summary",
            registrations=regs,
            callsigns=[payload.callsigns] if payload.callsigns else None,
            icao=airlines,
            start_date=start_date_str,
            end_date=end_date_str,
            user=payload.user,
            correlation_id=request.state.correlation_id,
            _queue_name=EXTERNAL_QUEUE,
        )

        if payload.from_pbi:
            filename = f"{random.randint(10000, 99999)}.json"
            filepath = Path(RESPONSES_PATH / filename)
            filepath.write_text('{"ok": True}', encoding="utf-8")

            return FileResponse(
                filepath,
                media_type="application/json",
                filename=filename,
                background=background_tasks,
            )
        return success_response(request=request, response=response, data=[None], msg="Process started successfully", status_code=status.HTTP_202_ACCEPTED)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.get(
    path="/airports",
    description="Start Airports process",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DefaultResponse[List[None]],
    responses=build_responses(
        include={status.HTTP_202_ACCEPTED, status.HTTP_404_NOT_FOUND, status.HTTP_500_INTERNAL_SERVER_ERROR}
    )
)
async def process_data_airports(request: Request, response: Response,
                       background_tasks: BackgroundTasks,
                       _payload: Annotated[RequestFRAirports, Query()]):
    payload = RequestFRAirports(
        **_payload.model_dump()
    )

    try:
        await request.state.arq.enqueue_job(
            "load_airports",
            codes=str_to_list(payload.codes),
            _queue_name=EXTERNAL_QUEUE,
        )

        if payload.from_pbi:
            filename = f"{random.randint(10000, 99999)}.json"
            filepath = Path(RESPONSES_PATH / filename)
            filepath.write_text('{"ok": True}', encoding="utf-8")

            return FileResponse(
                filepath,
                media_type="application/json",
                filename=filename,
                background=background_tasks,
            )

        return success_response(request=request, response=response, data=[None], msg="Process started successfully", status_code=status.HTTP_202_ACCEPTED)

    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)

