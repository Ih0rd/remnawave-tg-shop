from datetime import datetime, timezone
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
