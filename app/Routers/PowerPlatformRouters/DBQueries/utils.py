from sqlalchemy.ext.asyncio import AsyncSession

from Database import Aircraft, Engine
from Schemas import AircraftSchemaFull, EngineSchema, EngineTypeSchema, AirlineSchemaLight, TemplateSchemaLight, \
    PolicySchema, AircraftTechnicalDataSchema
from Schemas.PowerPlatform.BodySchemas.AircraftSchemas import CreateUpdateAircraftBody


async def get_aircraft_db_schema(session: AsyncSession, aircraft: Aircraft) -> AircraftSchemaFull:
    policy_db = [(p.policy_from, p.policy_to) for p in aircraft.policy if p.active]
    policy_db_from = policy_db_to = None
    lessee_lessor_db = [(l.lessee, l.lessor) for l in aircraft.lessee_lessors if l.active]
    lessee_db = lessor_db = None
    engines = []
    for engine in aircraft.engines:
        engine_type = await session.get(Engine, engine.engine_id)
        engines.append(
            EngineSchema(
                engine=EngineTypeSchema(
                    engine_id=engine_type.id,
                    engine_manufacture=engine_type.engine_manufacture,
                    engine_model=engine_type.engine_model
                ),
                position=engine.position,
                msn=engine.engine_msn
            )
        )
    if len(policy_db) > 0:
        policy_db_from, policy_db_to = policy_db[0]
    if len(lessee_lessor_db) > 0:
        lessee_db, lessor_db = lessee_lessor_db[0]
    aircraft_db = AircraftSchemaFull(
        aircraft_id=aircraft.id,
        registration=aircraft.registration,
        msn=aircraft.msn,
        mtow=aircraft.mtow,
        airline=AirlineSchemaLight(
            airline_id=aircraft.airline.id,
            airline_name=None,
            airline_icao=None
        ),
        template=TemplateSchemaLight(
            template_id=aircraft.template.id,
            template_name=None
        ),
        policy=[
            PolicySchema(
                policy_id=None,
                policy_from=policy_db_from,
                policy_to=policy_db_to,
            )
        ],
        engines=engines,
        agreed_value=aircraft.agreed_value,
        depreciation_rate=aircraft.depreciation_rate,
        depreciation_start_date=aircraft.depreciation_start_date,
        combined_single_limit=aircraft.combined_single_limit,
        hsl_deductible=aircraft.hsl_deductible,
        hd_deductible=aircraft.hd_deductible,
        lessee=lessee_db,
        lessor=lessor_db,
        technical_data=None
    )

    return aircraft_db


async def get_aircraft_payload_schema(session: AsyncSession, aircraft: CreateUpdateAircraftBody) -> AircraftSchemaFull:
    engines = []
    for engine in aircraft.engines:
        engine_type = await session.get(Engine, engine.engine_id)
        engines.append(
            EngineSchema(
                engine=EngineTypeSchema(
                    engine_id=engine_type.id,
                    engine_manufacture=engine_type.engine_manufacture,
                    engine_model=engine_type.engine_model
                ),
                position=engine.position,
                msn=engine.engine_msn,
            )
        )
    aircraft_payload = AircraftSchemaFull(
        aircraft_id=aircraft.aircraft_id,
        registration=aircraft.aircraft_registration,
        msn=aircraft.aircraft_msn,
        mtow=aircraft.mtow,
        airline=AirlineSchemaLight(
            airline_id=aircraft.airline_id,
            airline_name=None,
            airline_icao=None
        ),
        template=TemplateSchemaLight(
            template_id=aircraft.template_id,
            template_name=None
        ),
        policy=[
            PolicySchema(
                policy_id=None,
                policy_from=aircraft.policy_from,
                policy_to=aircraft.policy_to
            )
        ],
        engines=engines,
        agreed_value=aircraft.agreed_value,
        depreciation_rate=aircraft.depreciation_rate,
        depreciation_start_date=aircraft.depreciation_start_date,
        combined_single_limit=aircraft.combined_single_limit,
        hsl_deductible=aircraft.hsl_deductible,
        hd_deductible=aircraft.hd_deductible,
        lessee=aircraft.lessee,
        lessor=aircraft.lessor,
        technical_data=None
    )

    return aircraft_payload