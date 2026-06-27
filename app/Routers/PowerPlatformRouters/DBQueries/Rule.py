from typing import Optional, List
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from Database import Rule
from Schemas.PowerPlatform.DefaultSchemas import RuleSchema, ApplicationRulesSchema


async def query_rules(session: AsyncSession, application_id: Optional[UUID] = None) -> List[ApplicationRulesSchema]:
    try:
        stmt = select(Rule)
        if application_id:
            stmt = stmt.where(Rule.application_id == application_id)

        result = await session.execute(stmt)
        rules = result.scalars().all()

        app_dict: dict[UUID, list[RuleSchema]] = {}
        for rule in rules:
            if rule.application_id not in app_dict:
                app_dict[rule.application_id] = []
            app_dict[rule.application_id].append(
                RuleSchema(
                    rule_id=rule.id,
                    rule_name=rule.rule_name,
                    rule_description=rule.rule_description
                )
            )

        applications_list = [
            ApplicationRulesSchema(application_id=app_id, rules=rules_list)
            for app_id, rules_list in app_dict.items()
        ]

        return applications_list
    except Exception as _ex:
        raise _ex
