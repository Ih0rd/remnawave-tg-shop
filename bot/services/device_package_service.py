from __future__ import annotations

from datetime import datetime, timezone
from calendar import monthrange
from typing import Optional, Dict, Any

from aiogram import Bot
from aiogram.types import LabeledPrice
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import Settings
from db.dal import user_dal, user_device_package_dal, payment_dal
from bot.services.panel_api_service import PanelApiService


def _add_months_utc(dt: datetime, months: int) -> datetime:
    base = dt.astimezone(timezone.utc)
    month = base.month - 1 + months
    year = base.year + month // 12
    month = month % 12 + 1
    day = min(base.day, monthrange(year, month)[1])
    return base.replace(year=year, month=month, day=day)


class DevicePackageService:
    def __init__(self, settings: Settings, panel_service: PanelApiService, bot: Bot, i18n):
        self.settings = settings
        self.panel_service = panel_service
        self.bot = bot
        self.i18n = i18n

    async def get_effective_hwid_limit(self, session: AsyncSession, user_id: int) -> Optional[int]:
        base_limit = self.settings.USER_HWID_DEVICE_LIMIT
        if base_limit is None:
            return None
        active_packages = await user_device_package_dal.get_active_packages(session, user_id)
        return int(base_limit) + sum(int(p.added_devices) for p in active_packages)

    async def sync_expired_packages(self, session: AsyncSession, user_id: int) -> int:
        expired = await user_device_package_dal.get_expired_unhandled_packages(session, user_id)
        if not expired:
            return 0
        for pkg in expired:
            pkg.is_active = False
            pkg.expiry_notified_at = datetime.now(timezone.utc)
        await self._apply_limit_to_panel(session, user_id)
        await session.flush()
        return len(expired)

    async def _apply_limit_to_panel(self, session: AsyncSession, user_id: int) -> bool:
        db_user = await user_dal.get_user_by_id(session, user_id)
        if not db_user or not db_user.panel_user_uuid:
            return False
        limit = await self.get_effective_hwid_limit(session, user_id)
        payload: Dict[str, Any] = {"hwidDeviceLimit": limit}
        result = await self.panel_service.update_user_details_on_panel(db_user.panel_user_uuid, payload)
        return bool(result)

    async def activate_paid_package(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        package_key: str,
        months: int,
        provider: str,
        payment_id: int | None = None,
    ) -> Optional[datetime]:
        config = self.settings.addon_device_packages.get(package_key)
        if not config:
            return None
        added_devices = int(config["added_devices"])
        now = datetime.now(timezone.utc)
        expires_at = _add_months_utc(now, months)
        await user_device_package_dal.create_package(
            session,
            user_id=user_id,
            package_key=package_key,
            months=months,
            added_devices=added_devices,
            provider=provider,
            payment_id=payment_id,
            expires_at=expires_at,
        )
        await self._apply_limit_to_panel(session, user_id)
        return expires_at

    async def create_stars_invoice(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        package_key: str,
        months: int,
        stars_price: int,
        description: str,
    ) -> Optional[int]:
        payment_record_data = {
            "user_id": user_id,
            "amount": float(stars_price),
            "currency": "XTR",
            "status": "pending_stars",
            "description": description,
            "subscription_duration_months": months,
            "provider": "telegram_stars",
        }
        record = await payment_dal.create_payment_record(session, payment_record_data)
        await session.flush()
        payload = f"addon:{record.payment_id}:{package_key}:{months}"
        await self.bot.send_invoice(
            chat_id=user_id,
            title=description,
            description=description,
            payload=payload,
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label=description, amount=stars_price)],
        )
        return record.payment_id
