from typing import Annotated, List

from fastapi import status, Request, Query, Body, Response, UploadFile, File
from fastapi.responses import FileResponse

from Config import setup_logger
from settings import Router, OUTPUT_PATH
from Schemas import DefaultResponse, TemplateSchemaLight, TemplateSchemaFull, AircraftSchemaFull, AircraftSchemaLight, \
    EngineSchema, AdditionalAircraftInfoSchema, EngineTypeSchema, UpsertdelResponseSchema, CiriumAircraftSchema, \
    ExcelAircraftSchema
from Schemas.Enums import service
from Schemas.PowerPlatform.BodySchemas.AircraftSchemas import CreateAircraftTemplatesBody, CreateUpdateAircraftBody, \
    GetAircraftsFromCiriumBody, CreateAircraftsFromCiriumBody, CreateAircraftsFromExcelSchema
from Schemas.PowerPlatform.QuerySchemas.AircraftSchemas import GetAircraftQuery, GetEngineTypeQuery, \
    GetAircraftTemplateQuery, GetAircraftIDQuery
from Utils import DBProxy, success_response, error_response, warning_response, cache_key_first_non_null
from Utils.ResponsesFunc import build_responses
from .DBQueries.Aircrafts import query_aircrafts, query_get_engines_type, query_templates, query_create_template, \
    query_create_update_aircraft, query_aircraft_additional, query_get_engines, query_get_aircrafts_cirium, \
    query_create_aircrafts_cirium
from .DBQueries.AircraftsExcel import query_import_aircrafts, query_parse_aircrafts_excel

logger = setup_logger(name="powerplatform_aircrafts")

router = Router(
    prefix="/powerplatform/aircrafts",
    tags=[service.APITagsEnum.AIRCRAFTS],
)


@router.get(
    path="/templates/full",
    description="Get Aircraft templates",
    status_code=status.HTTP_200_OK,
    response_model=DefaultResponse[List[TemplateSchemaFull]],
    responses=build_responses(
        include={status.HTTP_200_OK, status.HTTP_404_NOT_FOUND, status.HTTP_500_INTERNAL_SERVER_ERROR}
    )
)
async def get_aircraft_template_full(request: Request, response: Response,
                                     _payload: Annotated[GetAircraftTemplateQuery, Query()]):
    db_proxy: DBProxy = request.state.db_proxy

    async def db_query(session):
        return await query_templates(session, _payload=_payload, full=True)

    try:
        cache_key = cache_key_first_non_null(name="aircraft_template:full", data=_payload.model_dump(),
                                             keys=("template_id", "template_name"),
                                             fallback="all")
        aircraft_template_data = await db_proxy.get_or_cache(
            key=cache_key,
            db_name="powerplatform",
            query_func=db_query,
            ttl=60
        )
        if len(aircraft_template_data) > 0:
            return success_response(request=request, response=response, data=aircraft_template_data,
                                    msg="Aircraft template(-s) retrieved successfully")
        return warning_response(request=request, response=response, msg="Aircraft template(-s) not found",
                                status_code=status.HTTP_404_NOT_FOUND)

    except ValueError:
        return warning_response(request=request, response=response, msg="Aircraft template(-s) not found",
                                status_code=status.HTTP_404_NOT_FOUND)
    except Exception as _ex:
        logger.error(f"Failed to get Aircraft template(-s): {_ex}")
        return error_response(request=request, response=response, exc=_ex)


@router.get(
    path="/templates/light",
    description="Get Aircraft templates",
    status_code=status.HTTP_200_OK,
    response_model=DefaultResponse[List[TemplateSchemaLight]],
    responses=build_responses(
        include={status.HTTP_200_OK, status.HTTP_404_NOT_FOUND, status.HTTP_500_INTERNAL_SERVER_ERROR}
    )
)
async def get_aircraft_template_light(request: Request, response: Response,
                                      _payload: Annotated[GetAircraftTemplateQuery, Query()]):
    db_proxy: DBProxy = request.state.db_proxy

    async def db_query(session):
        return await query_templates(session, _payload=_payload, full=False)

    try:
        cache_key = cache_key_first_non_null(name="aircraft_template:light", data=_payload.model_dump(),
                                             keys=(
                                                 "template_id", "template_name"),
                                             fallback="all")
        aircraft_template_data = await db_proxy.get_or_cache(
            key=cache_key,
            db_name="powerplatform",
            query_func=db_query,
            ttl=60
        )
        if len(aircraft_template_data) > 0:
            return success_response(request=request, response=response, data=aircraft_template_data,
                                    msg="Aircraft template(-s) retrieved successfully")
        return warning_response(request=request, response=response, msg="Aircraft template(-s) not found",
                                status_code=status.HTTP_404_NOT_FOUND)

    except ValueError:
        return warning_response(request=request, response=response, msg="Aircraft template(-s) not found",
                                status_code=status.HTTP_404_NOT_FOUND)
    except Exception as _ex:
        logger.error(f"Failed to get Aircraft template(-s): {_ex}")
        return error_response(request=request, response=response, exc=_ex)


@router.post(
    path="/templates",
    description="Create Aircraft template",
    status_code=status.HTTP_201_CREATED,
    response_model=DefaultResponse[List[UpsertdelResponseSchema]],
    responses=build_responses(
        include={status.HTTP_201_CREATED, status.HTTP_500_INTERNAL_SERVER_ERROR}
    )
)
async def create_aircraft_template(request: Request, response: Response,
                                   _payload: Annotated[CreateAircraftTemplatesBody, Body()]):
    db_proxy: DBProxy = request.state.db_proxy

    async def db_query(session):
        return await query_create_template(session, _payload=_payload)

    try:
        cache_key = f"template:{_payload.template_name}"
        result = await db_proxy.update_and_cache(
            key=cache_key,
            db_name="powerplatform",
            update_func=db_query,
            ttl=60
        )

        return success_response(request=request, response=response, msg="Aircraft template created successfully",
                                status_code=status.HTTP_201_CREATED, data=result)

    except Exception as _ex:
        logger.error(f"Failed to create Aircraft template: {_ex}")
        return error_response(request=request, response=response, exc=_ex)


@router.get(
    path="/full",
    description="Get Aircrafts Full",
    status_code=status.HTTP_200_OK,
    response_model=DefaultResponse[List[AircraftSchemaFull]],
    responses=build_responses(
        include={status.HTTP_200_OK, status.HTTP_404_NOT_FOUND, status.HTTP_500_INTERNAL_SERVER_ERROR}
    )
)
async def get_aircrafts_full(request: Request, response: Response, _payload: Annotated[GetAircraftQuery, Query()]):
    db_proxy: DBProxy = request.state.db_proxy

    async def db_query(session):
        return await query_aircrafts(session, full=True, _payload=_payload)

    try:
        cache_key = cache_key_first_non_null(name="aircraft:full", data=_payload.model_dump(),
                                             keys=(
                                                 "template_id", "template_name", "aircraft_registration", "aircraft_id",
                                                 "airline_id", "airline_name", "aircraft_msn"),
                                             fallback="all")
        aircraft_data = await db_proxy.get_or_cache(
            key=cache_key,
            db_name="powerplatform",
            query_func=db_query,
            ttl=60
        )
        if len(aircraft_data) > 0:
            return success_response(request=request, response=response, data=aircraft_data,
                                    msg="Aircraft retrieved successfully")
        return warning_response(request=request, response=response, msg="Aircraft not found",
                                status_code=status.HTTP_404_NOT_FOUND)

    except ValueError:
        return warning_response(request=request, response=response, msg="Aircraft not found",
                                status_code=status.HTTP_404_NOT_FOUND)
    except Exception as _ex:
        logger.error(f"Failed to get Aircraft: {_ex}")
        return error_response(request=request, response=response, exc=_ex)


@router.get(
    path="/light",
    description="Get Aircrafts Light",
    status_code=status.HTTP_200_OK,
    response_model=DefaultResponse[List[AircraftSchemaLight]],
    responses=build_responses(
        include={status.HTTP_200_OK, status.HTTP_404_NOT_FOUND, status.HTTP_500_INTERNAL_SERVER_ERROR}
    )
)
async def get_aircrafts_light(request: Request, response: Response, _payload: Annotated[GetAircraftQuery, Query()]):
    db_proxy: DBProxy = request.state.db_proxy

    async def db_query(session):
        return await query_aircrafts(session, full=False, _payload=_payload)

    try:
        cache_key = cache_key_first_non_null(name="aircraft:light", data=_payload.model_dump(),
                                             keys=(
                                                 "template_id", "template_name", "aircraft_registration", "aircraft_id",
                                                 "airline_id", "airline_name", "aircraft_msn"),
                                             fallback="all")

        aircraft_data = await db_proxy.get_or_cache(
            key=cache_key,
            db_name="powerplatform",
            query_func=db_query,
            ttl=60
        )
        if len(aircraft_data) > 0:
            return success_response(request=request, response=response, data=aircraft_data,
                                    msg="Aircraft retrieved successfully")
        return warning_response(request=request, response=response, msg="Aircraft not found",
                                status_code=status.HTTP_404_NOT_FOUND)

    except ValueError:
        return warning_response(request=request, response=response, msg="Aircraft not found",
                                status_code=status.HTTP_404_NOT_FOUND)
    except Exception as _ex:
        logger.error(f"Failed to get Aircraft: {_ex}")
        return error_response(request=request, response=response, exc=_ex)


@router.post(
    path="/",
    description="Create Aircraft",
    status_code=status.HTTP_201_CREATED,
    response_model=DefaultResponse[List[UpsertdelResponseSchema]],
    responses=build_responses(
        include={status.HTTP_201_CREATED, status.HTTP_500_INTERNAL_SERVER_ERROR}
    )
)
async def create_aircraft(request: Request, response: Response, _payload: Annotated[CreateUpdateAircraftBody, Body()]):
    db_proxy: DBProxy = request.state.db_proxy

    async def db_query(session):
        return await query_create_update_aircraft(session, _payload)

    try:
        cache_key = f"aircraft:{_payload.aircraft_registration}"
        result = await db_proxy.update_and_cache(
            key=cache_key,
            db_name="powerplatform",
            update_func=db_query,
            ttl=60
        )

        return success_response(request=request, response=response, msg="Aircraft created successfully",
                                status_code=status.HTTP_201_CREATED, data=result)

    except Exception as _ex:
        logger.error(f"Failed to create Aircraft: {_ex}")
        return error_response(request=request, response=response, exc=_ex)


@router.get(
    path="/engines/type",
    description="Get Engines Type",
    status_code=status.HTTP_200_OK,
    response_model=DefaultResponse[List[EngineTypeSchema]],
    responses=build_responses(
        include={status.HTTP_200_OK, status.HTTP_404_NOT_FOUND, status.HTTP_500_INTERNAL_SERVER_ERROR}
    )
)
async def get_engines_type(request: Request, response: Response, _payload: Annotated[GetEngineTypeQuery, Query()]):
    db_proxy: DBProxy = request.state.db_proxy

    async def db_query(session):
        return await query_get_engines_type(session, _payload)

    try:
        cache_key = cache_key_first_non_null(name="engine_type", data=_payload.model_dump(),
                                             keys=(
                                                 "aircraft_id", "engine_id", "engine_manufacture", "engine_type"),
                                             fallback="all")

        engine_data = await db_proxy.get_or_cache(
            key=cache_key,
            db_name="powerplatform",
            query_func=db_query,
            ttl=60
        )
        if len(engine_data) > 0:
            return success_response(request=request, response=response, data=engine_data,
                                    msg="Engine retrieved successfully")
        return warning_response(request=request, response=response, msg="Engine not found",
                                status_code=status.HTTP_404_NOT_FOUND)

    except ValueError:
        return warning_response(request=request, response=response, msg="Engine not found",
                                status_code=status.HTTP_404_NOT_FOUND)
    except Exception as _ex:
        logger.error(f"Failed to get Engine: {_ex}")
        return error_response(request=request, response=response, exc=_ex)


@router.get(
    path="/engines",
    description="Get Aircraft Engines",
    status_code=status.HTTP_200_OK,
    response_model=DefaultResponse[List[EngineSchema]],
    responses=build_responses(
        include={status.HTTP_200_OK, status.HTTP_404_NOT_FOUND, status.HTTP_500_INTERNAL_SERVER_ERROR}
    )
)
async def get_engines(request: Request, response: Response, _payload: Annotated[GetAircraftIDQuery, Query()]):
    db_proxy: DBProxy = request.state.db_proxy

    async def db_query(session):
        return await query_get_engines(session, _payload)

    try:
        cache_key = cache_key_first_non_null(name="engine", data=_payload.model_dump(),
                                             keys="aircraft_id",
                                             fallback="all")

        engine_data = await db_proxy.get_or_cache(
            key=cache_key,
            db_name="powerplatform",
            query_func=db_query,
            ttl=60
        )
        if len(engine_data) > 0:
            return success_response(request=request, response=response, data=engine_data,
                                    msg="Engine retrieved successfully")
        return warning_response(request=request, response=response, msg="Engine not found",
                                status_code=status.HTTP_404_NOT_FOUND)

    except ValueError:
        return warning_response(request=request, response=response, msg="Engine not found",
                                status_code=status.HTTP_404_NOT_FOUND)
    except Exception as _ex:
        logger.error(f"Failed to get Engine: {_ex}")
        return error_response(request=request, response=response, exc=_ex)


@router.get(
    path="/additional",
    description="Get Aircrafts additional information",
    status_code=status.HTTP_200_OK,
    response_model=DefaultResponse[List[AdditionalAircraftInfoSchema]],
    responses=build_responses(
        include={status.HTTP_200_OK, status.HTTP_404_NOT_FOUND, status.HTTP_500_INTERNAL_SERVER_ERROR}
    )
)
async def get_aircrafts_additional(request: Request, response: Response,
                                   _payload: Annotated[GetAircraftIDQuery, Query()]):
    payload = GetAircraftIDQuery(
        **_payload.model_dump()
    )

    db_proxy: DBProxy = request.state.db_proxy

    async def db_query(session):
        return await query_aircraft_additional(session, aircraft_id=payload.aircraft_id)

    try:
        cache_key = f"aircraft_additional:{payload.aircraft_id}"
        aircraft_data = await db_proxy.get_or_cache(
            key=cache_key,
            db_name="aixii_cirium",
            query_func=db_query,
            ttl=60
        )
        if len(aircraft_data) > 0:
            return success_response(request=request, response=response, data=aircraft_data,
                                    msg="Aircraft additional info retrieved successfully")
        return warning_response(request=request, response=response, msg="Aircraft not found",
                                status_code=status.HTTP_404_NOT_FOUND)

    except ValueError:
        return warning_response(request=request, response=response, msg="Aircraft not found",
                                status_code=status.HTTP_404_NOT_FOUND)
    except Exception as _ex:
        logger.error(f"Failed to get Aircraft additional info: {_ex}")
        return error_response(request=request, response=response, exc=_ex)


@router.post(
    path="/cirium",
    description="Get Aircrafts From Cirium",
    status_code=status.HTTP_200_OK,
    response_model=DefaultResponse[List[CiriumAircraftSchema]],
    responses=build_responses(
        include={status.HTTP_200_OK, status.HTTP_404_NOT_FOUND, status.HTTP_500_INTERNAL_SERVER_ERROR}
    )
)
async def get_aircrafts_cirium(request: Request, response: Response,
                               _payload: Annotated[GetAircraftsFromCiriumBody, Body()]):
    db_proxy: DBProxy = request.state.db_proxy

    async def db_query(session):
        return await query_get_aircrafts_cirium(session, _payload=_payload)

    try:
        cache_key = cache_key_first_non_null(name="aircraft:cirium", data=_payload.model_dump(),
                                             keys=("airlines_name"),
                                             fallback="all")

        aircraft_data = await db_proxy.get_or_cache(
            key=cache_key,
            db_name="cirium",
            query_func=db_query,
            ttl=60
        )
        if len(aircraft_data) > 0:
            return success_response(request=request, response=response, data=aircraft_data,
                                    msg="Aircraft retrieved successfully")
        return warning_response(request=request, response=response, msg="Aircraft not found",
                                status_code=status.HTTP_404_NOT_FOUND)

    except ValueError:
        return warning_response(request=request, response=response, msg="Aircraft not found",
                                status_code=status.HTTP_404_NOT_FOUND)
    except Exception as _ex:
        logger.error(f"Failed to get Aircraft: {_ex}")
        return error_response(request=request, response=response, exc=_ex)


@router.post(
    path="/cirium/create",
    description="Create Aircrafts From Cirium",
    status_code=status.HTTP_201_CREATED,
    response_model=DefaultResponse[List[UpsertdelResponseSchema]],
    responses=build_responses(
        include={status.HTTP_201_CREATED, status.HTTP_500_INTERNAL_SERVER_ERROR}
    )
)
async def create_aircraft_cirium(request: Request, response: Response,
                                 _payload: Annotated[CreateAircraftsFromCiriumBody, Body()]):
    db_proxy: DBProxy = request.state.db_proxy

    async def db_query(session):
        return await query_create_aircrafts_cirium(session, _payload)

    try:
        cache_key = f"aircraft:{_payload.registrations}"
        result = await db_proxy.update_and_cache(
            key=cache_key,
            db_name="powerplatform",
            update_func=db_query,
            ttl=60
        )

        return success_response(request=request, response=response, msg="Aircraft created successfully",
                                status_code=status.HTTP_201_CREATED, data=result)

    except Exception as _ex:
        logger.error(f"Failed to create Aircraft: {_ex}")
        return error_response(request=request, response=response, exc=_ex)


@router.post(
    path="/parse",
    description="Parse Aircrafts From Excel",
    status_code=status.HTTP_200_OK,
    response_model=DefaultResponse[List[ExcelAircraftSchema]],
    responses=build_responses(
        include={
            status.HTTP_200_OK,
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            status.HTTP_400_BAD_REQUEST
        }
    ),
)
async def parse_aircraft_manuals(request: Request, response: Response, file: Annotated[UploadFile, File(...)]):
    db_proxy: DBProxy = request.state.db_proxy

    async def db_query(session):
        return await query_parse_aircrafts_excel(file=file)

    try:
        cache_key = f"aircraft_manual_parse:{file.filename}"

        result = await db_proxy.get_or_cache(
            key=cache_key,
            db_name="powerplatform",
            query_func=db_query,
            ttl=60,
        )

        if len(result) > 0:
            return success_response(request=request, response=response, msg="Aircrafts parsed successfully",
                                    status_code=status.HTTP_200_OK, data=result)
        return warning_response(request=request, response=response, msg="Aircraft not parsed",
                                status_code=status.HTTP_400_BAD_REQUEST)

    except Exception as _ex:
        logger.error(f"Failed to parse aircrafts: {_ex}")

        return error_response(request=request, response=response, exc=_ex)


@router.post(
    path="/import",
    description="Import Aircrafts From Excel",
    status_code=status.HTTP_201_CREATED,
    response_model=DefaultResponse[List[UpsertdelResponseSchema]],
    responses=build_responses(
        include={
            status.HTTP_201_CREATED,
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        }
    ),
)
async def import_aircraft_manuals(request: Request, response: Response, _payload: Annotated[List[CreateAircraftsFromExcelSchema], Body()]):
    db_proxy: DBProxy = request.state.db_proxy

    async def db_query(session):
        return await query_import_aircrafts(_payload=_payload, session=session)

    try:
        cache_key = f"aircraft_manual_import:{_payload[0].msn}"

        result = await db_proxy.get_or_cache(
            key=cache_key,
            db_name="powerplatform",
            query_func=db_query,
            ttl=60,
        )

        return success_response(request=request, response=response, msg="Aircrafts imported successfully",
                                status_code=status.HTTP_201_CREATED, data=result)

    except Exception as _ex:
        logger.error(f"Failed to imported aircrafts: {_ex}")

        return error_response(request=request, response=response, exc=_ex)


@router.get(
    path="/template_file",
    description="Download Aircrafts Template File",
    status_code=status.HTTP_200_OK,
    responses=build_responses(
        include={
            status.HTTP_200_OK,
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        }
    ),
)
async def export_aircrafts_file(request: Request, response: Response):
    try:
        file_path = OUTPUT_PATH / "Aircrafts Template.xlsx"
        file_name = "Aircrafts Template.xlsx"

        return FileResponse(
            path=file_path,
            filename=file_name,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    except Exception as _ex:
        logger.error(f"Failed to export aircrafts file: {_ex}")

        return error_response(
            request=request,
            response=response,
            exc=_ex,
        )
