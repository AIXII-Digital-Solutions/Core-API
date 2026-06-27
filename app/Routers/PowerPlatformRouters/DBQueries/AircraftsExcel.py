import tempfile
from datetime import datetime
from typing import Optional, List

import pandas as pd
from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from Database import AircraftManual, AircraftEngineManual, AircraftTemplate, AircraftDataSourceEnum, \
    AircraftInsuredStatusEnum, EnginePositionEnum, Airline, Engine, CiriumAircrafts, DatabaseClient
from enqueue import enqueue_external
from Schemas import UpsertdelResponseSchema, UpsertdelStatusEnum, ExcelAircraftSchema
from Schemas.PowerPlatform.BodySchemas.AircraftSchemas import CreateAircraftsFromExcelSchema


async def fuzzy_find_one(session: AsyncSession, model, field, value: str, threshold: float = 0.4):
    result = await session.execute(
        select(
            model,
            func.similarity(
                field,
                value
            ).label("score")
        )
        .where(field.is_not(None))
        .order_by(
            func.similarity(
                field,
                value
            ).desc()
        )
        .limit(1)
    )

    row = result.first()

    if not row:
        return None

    obj, score = row

    if score < threshold:
        return None

    return obj


ENGINE_POSITION_MAP = {
    2: [
        EnginePositionEnum.LEFT1,
        EnginePositionEnum.RIGHT1,
    ],
    3: [
        EnginePositionEnum.LEFT1,
        EnginePositionEnum.CENTER,
        EnginePositionEnum.RIGHT1,
    ],
    4: [
        EnginePositionEnum.LEFT1,
        EnginePositionEnum.LEFT2,
        EnginePositionEnum.RIGHT1,
        EnginePositionEnum.RIGHT2,
    ],
}


def parse_bool(value) -> bool:
    if pd.isna(value):
        return False

    if isinstance(value, bool):
        return value

    value = str(value).strip().lower()

    return value in {
        "true",
        "1",
        "yes",
        "y",
        "fixed",
    }


def parse_float(value) -> Optional[float]:
    if pd.isna(value) or value == "":
        return None

    return float(str(value).replace(",", "").strip())


def parse_int(value) -> Optional[int]:
    if pd.isna(value) or value == "":
        return None

    return int(float(value))


def parse_date(value):
    if pd.isna(value) or value == "":
        return None

    if isinstance(value, datetime):
        return value.date()

    return pd.to_datetime(value).date()


async def get_airline_id(session: AsyncSession, airline_name: Optional[str]) -> Optional[int]:
    if not airline_name or pd.isna(airline_name):
        return None

    airline_name = str(airline_name).strip()

    airline = await session.scalar(
        select(Airline)
        .where(
            Airline.airline_name.op("%")(airline_name)
        )
        .order_by(
            func.similarity(
                Airline.airline_name,
                airline_name
            ).desc()
        )
        .limit(1)
    )

    return airline.id if airline else None


async def get_cirium_aircraft(client: DatabaseClient, msn: Optional[int]) -> Optional[CiriumAircrafts]:
    if not msn:
        return None

    async with client.session("cirium") as session:
        return await session.scalar(
            select(CiriumAircrafts).where(
                CiriumAircrafts.Serial_Number == str(msn),
                CiriumAircrafts.revision_id == (
                    select(func.max(CiriumAircrafts.revision_id))
                    .scalar_subquery()
                )
            )
        )


async def get_template_id(session: AsyncSession, cirium_aircraft: Optional[CiriumAircrafts]) -> Optional[int]:
    if not cirium_aircraft:
        return None

    template_name = (
        f"{cirium_aircraft.Manufacturer} "
        f"{cirium_aircraft.Series}"
    ).strip()

    template = await session.scalar(
        select(AircraftTemplate).where(
            AircraftTemplate.template_name.ilike(
                template_name
            )
        )
    )

    return template.id if template else None


from sqlalchemy import select, func


async def get_engine_id(session: AsyncSession, cirium_aircraft: Optional[CiriumAircrafts]) -> Optional[int]:
    if not cirium_aircraft:
        return None

    manufacturer = str(cirium_aircraft.Engine_Manufacturer or "").strip()

    model = str(cirium_aircraft.Engine_Master_Series or "").strip()

    engine = await session.scalar(
        select(Engine)
        .where(
            Engine.engine_manufacture.op("%")(
                manufacturer
            ),
            Engine.engine_model.op("%")(model),
        )
        .order_by(
            (
                    func.similarity(
                        Engine.engine_manufacture,
                        manufacturer,
                    )
                    +
                    func.similarity(
                        Engine.engine_model,
                        model,
                    )
            ).desc()
        )
        .limit(1)
    )

    return engine.id if engine else None


async def build_engines(session: AsyncSession, aircraft: CreateAircraftsFromExcelSchema, cirium_aircraft: Optional[CiriumAircrafts]) -> list[
    AircraftEngineManual]:

    engine_msns = list(filter(None, [
        aircraft.engine_msn_1,
        aircraft.engine_msn_2,
        aircraft.engine_msn_3,
        aircraft.engine_msn_4,
    ]))

    engine_count = len(engine_msns)

    if engine_count not in ENGINE_POSITION_MAP:
        return []

    positions = ENGINE_POSITION_MAP[engine_count]

    engine_id = await get_engine_id(session, cirium_aircraft)

    engines: list[AircraftEngineManual] = []

    for engine_msn, position in zip(engine_msns, positions):
        engines.append(
            AircraftEngineManual(
                engine_id=engine_id,
                position=position,
                engine_msn=engine_msn,
            )
        )

    return engines


def nan_to_none(value):
    return None if pd.isna(value) else value


async def query_parse_aircrafts_excel(file: UploadFile) -> List[ExcelAircraftSchema]:
    with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
        contents = await file.read()
        tmp.write(contents)
        tmp_path = tmp.name

    df = pd.read_excel(tmp_path, header=0, skiprows=[1])

    aircrafts: List[ExcelAircraftSchema] = []

    for _, row in df.iterrows():
        aircraft = ExcelAircraftSchema(
            registration=nan_to_none(row.get("Registration")),
            msn=str(parse_int(row.get("MSN"))) if parse_int(row.get("MSN")) else None,
            airline=nan_to_none(row.get("Airline")),
            mtow=parse_int(row.get("MTOW, kgs")),

            av_fixed=parse_bool(row.get("Agreed value fixed")),
            agreed_value=parse_int(row.get("Agreed value, $")),
            csl=parse_int(row.get("Combined single limit, $")),

            hsl_deductible=parse_int(row.get("HSL deductible, $")),
            hd_deductible=parse_int(row.get("HD deductible, $")),

            depreciation_rate=parse_float(row.get("Depreciation rate, %")),
            depreciation_start_date=parse_date(row.get("Depreciation start date")),

            policy_start=parse_date(row.get("Policy start date")),
            policy_end=parse_date(row.get("Policy end date")),

            lessee=nan_to_none(row.get("Lessee")),
            lessor=nan_to_none(row.get("Lessor")),

            engine_msn_1=nan_to_none(row.get("Engine MSN 1")),
            engine_msn_2=nan_to_none(row.get("Engine MSN 2")),
            engine_msn_3=nan_to_none(row.get("Engine MSN 3")),
            engine_msn_4=nan_to_none(row.get("Engine MSN 4")),
        )

        aircrafts.append(aircraft)

    return aircrafts



async def query_import_aircrafts(session: AsyncSession, _payload: List[CreateAircraftsFromExcelSchema]) -> List[UpsertdelResponseSchema]:
    import asyncio
    payload = [
        CreateAircraftsFromExcelSchema(**p.model_dump())
        for p in _payload
    ]

    created = 0
    updated = 0

    client: DatabaseClient = DatabaseClient()

    for item in payload:
        msn = parse_int(item.msn)

        cirium_aircraft = await get_cirium_aircraft(client, msn)

        aircraft_manual = await session.scalar(
            select(AircraftManual)
            .options(
                selectinload(AircraftManual.engines)
            )
            .where(
                AircraftManual.msn == msn
            )
        )

        is_new = aircraft_manual is None

        if is_new:
            aircraft_manual = AircraftManual()

        aircraft_manual.registration = item.registration
        aircraft_manual.msn = msn
        aircraft_manual.airline_id = await get_airline_id(session, item.airline)
        aircraft_manual.mtow = item.mtow
        aircraft_manual.template_id = await get_template_id(session, cirium_aircraft)

        aircraft_manual.av_fixed = item.av_fixed
        aircraft_manual.agreed_value = item.agreed_value
        aircraft_manual.combined_single_limit = item.csl

        aircraft_manual.hsl_deductible = item.hsl_deductible
        aircraft_manual.hd_deductible = item.hd_deductible

        aircraft_manual.depreciation_rate = item.depreciation_rate
        aircraft_manual.depreciation_start_date = item.depreciation_start_date

        aircraft_manual.policy_start = item.policy_start
        aircraft_manual.policy_end = item.policy_end

        aircraft_manual.lessee = item.lessee
        aircraft_manual.lessor = item.lessor

        aircraft_manual.data_source = AircraftDataSourceEnum.MANUAL
        aircraft_manual.status = AircraftInsuredStatusEnum.NOT_INSURED
        aircraft_manual.in_dashboard = True

        new_engines = await build_engines(
            session=session,
            aircraft=item,
            cirium_aircraft=cirium_aircraft
        )

        aircraft_manual.engines.clear()
        aircraft_manual.engines.extend(new_engines)

        if is_new:
            session.add(aircraft_manual)
            created += 1
        else:
            updated += 1

        await session.flush()

        manual_id = aircraft_manual.id

        await enqueue_external("update_create_aircraft_manual", target=manual_id)

    await session.commit()

    return [
        UpsertdelResponseSchema(
            status=UpsertdelStatusEnum.CREATED,
        )
    ]
