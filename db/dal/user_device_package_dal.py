from datetime import datetime, timedelta, timezone
from typing import List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import UserDevicePackage


async def create_package(
    session: AsyncSession,
    *,
    user_id: int,
    package_key: str,
    months: int,
    added_devices: int,
    provider: str,
    payment_id: int | None,
    expires_at: datetime,
) -> UserDevicePackage:
    model = UserDevicePackage(
        user_id=user_id,
        package_key=package_key,
        months=months,
        added_devices=added_devices,
        provider=provider,
        payment_id=payment_id,
        expires_at=expires_at,
        is_active=True,
    )
    session.add(model)
    await session.flush()
    await session.refresh(model)
    return model


async def get_active_packages(session: AsyncSession, user_id: int) -> List[UserDevicePackage]:
    now = datetime.now(timezone.utc)
    stmt = (
        select(UserDevicePackage)
        .where(
            UserDevicePackage.user_id == user_id,
            UserDevicePackage.is_active.is_(True),
            UserDevicePackage.expires_at > now,
        )
        .order_by(UserDevicePackage.expires_at.asc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_expired_unhandled_packages(session: AsyncSession, user_id: int) -> List[UserDevicePackage]:
    now = datetime.now(timezone.utc)
    stmt = (
        select(UserDevicePackage)
        .where(
            UserDevicePackage.user_id == user_id,
            UserDevicePackage.is_active.is_(True),
            UserDevicePackage.expires_at <= now,
        )
        .order_by(UserDevicePackage.expires_at.asc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_user_ids_with_expired_unhandled_packages(session: AsyncSession) -> List[int]:
    now = datetime.now(timezone.utc)
    stmt = (
        select(UserDevicePackage.user_id)
        .where(
            UserDevicePackage.is_active.is_(True),
            UserDevicePackage.expires_at <= now,
        )
        .distinct()
    )
    result = await session.execute(stmt)
    return [int(row[0]) for row in result.all() if row and row[0] is not None]


async def get_user_ids_with_packages_expiring_in_days(session: AsyncSession, days_left: int) -> List[int]:
    if days_left <= 0:
        return []
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    window_start = now + timedelta(days=days_left - 1)
    window_end = now + timedelta(days=days_left)
    stmt = (
        select(UserDevicePackage.user_id)
        .where(
            UserDevicePackage.is_active.is_(True),
            UserDevicePackage.expires_at > window_start,
            UserDevicePackage.expires_at <= window_end,
            (
                UserDevicePackage.expiry_notified_at.is_(None)
                | (UserDevicePackage.expiry_notified_at < day_start)
            ),
        )
        .distinct()
    )
    result = await session.execute(stmt)
    return [int(row[0]) for row in result.all() if row and row[0] is not None]
