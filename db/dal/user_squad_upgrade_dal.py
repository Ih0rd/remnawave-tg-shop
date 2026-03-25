from datetime import datetime, timezone
from typing import List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import UserSquadUpgrade


async def create_upgrade(
    session: AsyncSession,
    *,
    user_id: int,
    months: int,
    provider: str,
    payment_id: int | None,
    from_squads: str | None,
    to_squad_uuid: str,
    expires_at: datetime,
) -> UserSquadUpgrade:
    model = UserSquadUpgrade(
        user_id=user_id,
        months=months,
        provider=provider,
        payment_id=payment_id,
        from_squads=from_squads,
        to_squad_uuid=to_squad_uuid,
        expires_at=expires_at,
        is_active=True,
    )
    session.add(model)
    await session.flush()
    await session.refresh(model)
    return model


async def get_active_upgrades(session: AsyncSession, user_id: int) -> List[UserSquadUpgrade]:
    now = datetime.now(timezone.utc)
    stmt = (
        select(UserSquadUpgrade)
        .where(
            UserSquadUpgrade.user_id == user_id,
            UserSquadUpgrade.is_active.is_(True),
            UserSquadUpgrade.expires_at > now,
        )
        .order_by(UserSquadUpgrade.expires_at.asc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_expired_unhandled_upgrades(session: AsyncSession, user_id: int) -> List[UserSquadUpgrade]:
    now = datetime.now(timezone.utc)
    stmt = (
        select(UserSquadUpgrade)
        .where(
            UserSquadUpgrade.user_id == user_id,
            UserSquadUpgrade.is_active.is_(True),
            UserSquadUpgrade.expires_at <= now,
        )
        .order_by(UserSquadUpgrade.expires_at.asc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_user_ids_with_expired_unhandled_upgrades(session: AsyncSession) -> List[int]:
    now = datetime.now(timezone.utc)
    stmt = (
        select(UserSquadUpgrade.user_id)
        .where(
            UserSquadUpgrade.is_active.is_(True),
            UserSquadUpgrade.expires_at <= now,
        )
        .distinct()
    )
    result = await session.execute(stmt)
    return [int(row[0]) for row in result.all() if row and row[0] is not None]
