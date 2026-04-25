from datetime import datetime, timedelta, timezone
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


async def get_user_ids_with_upgrades_expiring_in_days(session: AsyncSession, days_left: int) -> List[int]:
    if days_left <= 0:
        return []
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    window_start = now + timedelta(days=days_left - 1)
    window_end = now + timedelta(days=days_left)
    stmt = (
        select(UserSquadUpgrade.user_id)
        .where(
            UserSquadUpgrade.is_active.is_(True),
            UserSquadUpgrade.expires_at > window_start,
            UserSquadUpgrade.expires_at <= window_end,
            (
                UserSquadUpgrade.expiry_notified_at.is_(None)
                | (UserSquadUpgrade.expiry_notified_at < day_start)
            ),
        )
        .distinct()
    )
    result = await session.execute(stmt)
    return [int(row[0]) for row in result.all() if row and row[0] is not None]
