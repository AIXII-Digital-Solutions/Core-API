from base64 import b64decode, b64encode
from typing import List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from Database import Asset
from Schemas import UpsertdelStatusEnum
from Schemas.PowerPlatform.BodySchemas.DefaultSchemas import ApplicationFileLoadBody
from Schemas.PowerPlatform.DefaultSchemas import AssetSchema, UpsertdelResponseSchema
from Schemas.PowerPlatform.QuerySchemas.ApplicationSchemas import GetApplicationFileQuery


async def query_load_file(session: AsyncSession, _payload: ApplicationFileLoadBody) -> List[UpsertdelResponseSchema]:
    payload = ApplicationFileLoadBody(
        **_payload.model_dump()
    )

    try:
        header, encoded = payload.file_data.split(",", 1)
        mime_type = header.split(";")[0].replace("data:", "")
        decoded_bytes = b64decode(encoded)

        file = Asset(
            asset_name=payload.file_name,
            asset_description=payload.file_description,
            mime_type=mime_type,
            base64=decoded_bytes
        )

        session.add(file)
        await session.commit()

        return [UpsertdelResponseSchema(status=UpsertdelStatusEnum.CREATED)]

    except Exception as _ex:
        raise _ex


async def query_file(session: AsyncSession, _payload: GetApplicationFileQuery) -> List[AssetSchema]:
    payload = GetApplicationFileQuery(
        **_payload.model_dump()
    )

    try:
        stmt = select(Asset)
        if payload.file_name:
            stmt = stmt.where(Asset.asset_name == payload.file_name)
        if payload.file_id:
            stmt = stmt.where(Asset.id == payload.file_id)

        result = await session.execute(stmt)
        file: Asset = result.scalars().one_or_none()
        if file is None:
            raise ValueError("File not found")

        encoded = b64encode(file.base64).decode()
        data_uri = f"data:{file.mime_type};base64,{encoded}"

        response = AssetSchema(
            file_name=file.asset_name,
            file_id=file.id,
            file_description=file.asset_description,
            file_data=data_uri
        )

        return [response]

    except Exception as _ex:
        raise _ex
