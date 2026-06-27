from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from Database import Font, Application, UserDeviceSetting, ApplicationAppearance
from Schemas import OSTypeEnum, AppearanceEnum, UpsertdelResponseSchema, UpsertdelStatusEnum
from Schemas.PowerPlatform.DefaultSchemas import FontSchema, ApplicationSchema, ApplicationAppearanceSchema
from Schemas.PowerPlatform.QuerySchemas.ApplicationSchemas import GetApplicationIdQuery, DeviceInfo, \
    UpsertAppearanceQuery
from Utils import map_asset


async def query_apps(session: AsyncSession, _payload: GetApplicationIdQuery) -> List[ApplicationSchema]:
    payload = GetApplicationIdQuery(
        **_payload.model_dump()
    )

    try:
        stmt = (
            select(Application)
            .options(
                selectinload(Application.asset)
            )
        )
        if payload.application_id:
            stmt = stmt.where(Application.application_id == payload.application_id)

        result = await session.execute(stmt)
        apps = result.scalars().all()

        apps_list = []

        for app in apps:
            apps_list.append(
                ApplicationSchema(
                    application_id=app.application_id,
                    application_name=app.application_name,
                    application_description=app.application_description,
                    application_status=app.status,
                    asset=map_asset(app.asset)
                )
            )

        return apps_list
    except Exception as _ex:
        raise _ex



async def query_fonts(session: AsyncSession, _payload: DeviceInfo) -> List[FontSchema]:
    payload = DeviceInfo(
        **_payload.model_dump()
    )

    try:
        appearance: Optional[int] = None
        stmt = select(Font)
        if payload.screen_size:
            stmt = stmt.where(Font.screen_size == payload.screen_size)
        if payload.user_id:
            user_stmt = (
                select(ApplicationAppearance.appearance_type)
                .join(
                    UserDeviceSetting,
                    UserDeviceSetting.appearance_id == ApplicationAppearance.id
                )
                .where(
                    UserDeviceSetting.user_id == payload.user_id,
                    UserDeviceSetting.os_type == payload.os_type
                )
            )
            user_result = await session.execute(user_stmt)
            appearance = user_result.scalars().first()

        result = await session.execute(stmt)
        fonts = result.scalars().all()

        fonts_list: List[FontSchema] = []

        for font in fonts:
            if payload.os_type in {OSTypeEnum.IOS, OSTypeEnum.ANDROID}:
                font_size = font.font_size_alternative if font.font_size_alternative is not None else font.font_size
            else:
                font_size = font.font_size

            if appearance == AppearanceEnum.DARK:
                font_color = font.font_color_alternative if font.font_color_alternative else font.font_color
            else:
                font_color = font.font_color

            fonts_list.append(
                FontSchema(
                    font_name=font.font_name,
                    font_size=font_size,
                    font_color=font_color,
                    font_weight=font.font_weight,
                    screen_size=font.screen_size,
                    usage_name=font.usage_name
                )
            )

        return fonts_list


    except Exception as _ex:
        raise _ex


async def query_get_appearance(session: AsyncSession, _payload: DeviceInfo) -> List[ApplicationAppearanceSchema]:
    payload = DeviceInfo(
        **_payload.model_dump()
    )

    stmt = (
        select(ApplicationAppearance)
        .join(
            UserDeviceSetting,
            UserDeviceSetting.appearance_id == ApplicationAppearance.id
        )
        .where(
            UserDeviceSetting.user_id == payload.user_id,
            UserDeviceSetting.os_type == payload.os_type
        )
    )

    result = await session.execute(stmt)
    appearance: Optional[ApplicationAppearance] = result.scalars().first()
    if not appearance:

        default_stmt = (
            select(ApplicationAppearance)
            .where(
                ApplicationAppearance.appearance_type == AppearanceEnum.LIGHT
            )
        )

        default_result = await session.execute(default_stmt)

        default_appearance = default_result.scalars().first()

        if not default_appearance:
            raise ValueError("Default appearance LIGHT not found")

        new_setting = UserDeviceSetting(
            user_id=payload.user_id,
            os_type=payload.os_type,
            appearance_id=default_appearance.id
        )

        session.add(new_setting)

        await session.commit()
        await session.refresh(new_setting)

        appearance = default_appearance

    appearance_result = [
        ApplicationAppearanceSchema(
            appearance=appearance.appearance_type,
            main_color=appearance.main_color,
            secondary_color=appearance.secondary_color,
            app_color=appearance.app_color,
            background_color=appearance.background_color,
            field_color=appearance.field_color,
            button_color=appearance.button_color
        )
    ]

    return appearance_result


async def query_upsert_appearance(session: AsyncSession, _payload: UpsertAppearanceQuery) -> List[UpsertdelResponseSchema]:
    payload = UpsertAppearanceQuery(
        **_payload.model_dump()
    )

    stmt = (
        select(ApplicationAppearance)
        .where(
            ApplicationAppearance.appearance_type == payload.appearance
        )
    )

    result = await session.execute(stmt)

    appearance: Optional[ApplicationAppearance] = result.scalars().first()

    if appearance:

        appearance.main_color = payload.main_color
        appearance.secondary_color = payload.secondary_color
        appearance.app_color = payload.app_color
        appearance.background_color = payload.background_color
        appearance.field_color = payload.field_color
        appearance.button_color = payload.button_color

        await session.commit()
        await session.refresh(appearance)

        return [UpsertdelResponseSchema(status=UpsertdelStatusEnum.UPDATED)]


    new_appearance = ApplicationAppearance(
        appearance_type=payload.appearance,
        main_color=payload.main_color,
        secondary_color=payload.secondary_color,
        app_color=payload.app_color,
        background_color=payload.background_color,
        field_color=payload.field_color,
        button_color=payload.button_color,
    )

    session.add(new_appearance)

    await session.commit()
    await session.refresh(new_appearance)

    return [UpsertdelResponseSchema(status=UpsertdelStatusEnum.CREATED)]


