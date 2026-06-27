from base64 import b64decode
from typing import List
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from Database import Airline, User, Asset, UserAirlineAccess
from Schemas import UserSchemaLight, AirlineUsersSchemaLight, AirlineUsersSchemaFull, UpsertdelResponseSchema, \
    UpsertdelStatusEnum
from Schemas.PowerPlatform.BodySchemas.AirlineSchemas import CreateAirlinesBody
from Schemas.PowerPlatform.QuerySchemas.AirlineSchemas import GetAirlineQuery
from Utils import map_asset


async def query_create_airline(session: AsyncSession, _payload: CreateAirlinesBody) -> List[UpsertdelResponseSchema]:
    payload = CreateAirlinesBody(
        **_payload.model_dump()
    )

    try:
        asset = None

        stmt = select(Airline).where(Airline.icao == payload.airline_icao)
        existing = await session.execute(stmt)
        if existing.scalar_one_or_none():
            raise ValueError("Airline already exists")

        if payload.file_data:
            header, encoded = payload.file_data.split(",", 1)
            mime_type = header.split(";")[0].replace("data:", "")
            decoded_bytes = b64decode(encoded)

            asset = Asset(
                asset_name=f"{payload.airline_name}",
                asset_description=None,
                mime_type=mime_type,
                base64=decoded_bytes
            )

        airline = Airline(
            airline_name=payload.airline_name,
            icao=payload.airline_icao,
            asset=asset
        )

        session.add(airline)
        await session.flush()

        if payload.user_id:
            stmt = select(User.user_id).where(User.user_id == payload.user_id)
            result = await session.execute(stmt)
            user_id_db = result.scalar_one_or_none()

            if not user_id_db:
                raise ValueError("User not found")

            session.add(
                UserAirlineAccess(
                    user_id=user_id_db,
                    airline_id=airline.id
                )
            )

        await session.commit()
        return [UpsertdelResponseSchema(status=UpsertdelStatusEnum.CREATED)]
    except Exception as _ex:
        raise _ex


async def map_airline_to_schema(airline, full: bool) -> AirlineUsersSchemaFull | AirlineUsersSchemaLight:
    users_list = [
        UserSchemaLight(
            user_id=user.user_id,
            user_mail=user.mail,
            user_displayname=user.display_name
        )
        for user in airline.users
    ]
    if full:
        response = AirlineUsersSchemaFull(
            airline_id=airline.id,
            airline_name=airline.airline_name,
            airline_icao=airline.icao,
            airline_iata=airline.iata,
            asset=map_asset(asset=airline.asset),
            users=users_list
        )
        return response
    else:
        response = AirlineUsersSchemaLight(
            airline_id=airline.id,
            airline_name=airline.airline_name,
            airline_icao=airline.icao,
            airline_iata=airline.iata,
            users=users_list
        )
        return response


async def get_by_user_id(session: AsyncSession, user_id: UUID, full: bool) -> List[AirlineUsersSchemaFull] | List[
    AirlineUsersSchemaLight]:
    stmt = (
        select(User)
        .options(
            selectinload(User.airlines)
            .selectinload(Airline.asset),
            selectinload(User.airlines)
            .selectinload(Airline.users)
        )
        .where(User.user_id == user_id)
        .order_by(Airline.id)
    )

    result = await session.execute(stmt)
    user = result.scalar_one_or_none()
    if not user:
        raise ValueError("User not found")

    return [await map_airline_to_schema(airline, full) for airline in user.airlines]


async def get_by_airline(session: AsyncSession, full: bool, airline_id: int | None = None,
                         airline_name: str | None = None) -> List[AirlineUsersSchemaFull] | List[
    AirlineUsersSchemaLight]:
    stmt = select(Airline).options(
        selectinload(Airline.asset),
        selectinload(Airline.users)
    ).order_by(Airline.id)
    if airline_id:
        stmt = stmt.where(Airline.id == airline_id)
    if airline_name:
        stmt = stmt.where(Airline.airline_name == airline_name)

    result = await session.execute(stmt)
    airline = result.scalar_one_or_none()
    if not airline:
        raise ValueError("Airline not found")

    return [await map_airline_to_schema(airline, full)]


async def get_all_airlines(session: AsyncSession, full: bool) -> List[AirlineUsersSchemaFull] | List[
    AirlineUsersSchemaLight]:
    stmt = (
        select(Airline)
        .options(
            selectinload(Airline.asset),
            selectinload(Airline.users)
        )
        .order_by(Airline.airline_name)
    )
    result = await session.execute(stmt)
    airlines = result.scalars().all()
    if not airlines:
        raise ValueError("Airline not found")

    return [await map_airline_to_schema(airline, full) for airline in airlines]


async def query_airline(session: AsyncSession, full: bool, _payload: GetAirlineQuery) -> List[AirlineUsersSchemaFull] | List[AirlineUsersSchemaLight]:
    payload = GetAirlineQuery(
        **_payload.model_dump()
    )

    if payload.user_id:
        return await get_by_user_id(session, payload.user_id, full)
    if payload.airline_id or payload.airline_name:
        return await get_by_airline(session, full, payload.airline_id, payload.airline_name)
    return await get_all_airlines(session, full)
