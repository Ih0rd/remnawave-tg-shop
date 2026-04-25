from __future__ import annotations

from datetime import datetime, timedelta, timezone
from calendar import monthrange
from typing import Optional, Dict, Any
import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
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

    def _base_limit_label(self, lang: str) -> str:
        value = self.settings.USER_HWID_DEVICE_LIMIT
        unlimited_text = "unlimited"
        if self.i18n:
            try:
                unlimited_text = self.i18n.gettext(lang, "devices_unlimited_label")
            except Exception:
                pass
        if value is None:
            return unlimited_text
        try:
            numeric = int(value)
        except (TypeError, ValueError):
            return str(value)
        if numeric <= 0:
            return unlimited_text
        return str(numeric)

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

    async def process_expired_packages_and_notify(self, session: AsyncSession, user_id: int) -> int:
        expired_count = await self.sync_expired_packages(session, user_id)
        if expired_count <= 0:
            return 0
        await session.commit()
        db_user = await user_dal.get_user_by_id(session, user_id)
        lang = self.settings.DEFAULT_LANGUAGE
        if db_user and db_user.language_code:
            lang = db_user.language_code
        if self.i18n:
            try:
                text = self.i18n.gettext(
                    lang,
                    "extra_devices_expired_and_reset",
                    base_limit=self._base_limit_label(lang),
                )
            except Exception:
                text = "Your additional devices package has expired."
        else:
            text = "Your additional devices package has expired."
        try:
            button_text = "📱 Продлить пакет устройств"
            if self.i18n:
                try:
                    button_text = self.i18n.gettext(lang, "buy_extra_devices_button")
                except Exception:
                    pass
            renewal_markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=button_text, callback_data="main_action:buy_extra_devices")]
                ]
            )
            await self.bot.send_message(user_id, text, reply_markup=renewal_markup)
        except Exception as e:
            logging.error(f"Failed to send addon package expiry notification to user {user_id}: {e}")
        return expired_count

    async def process_upcoming_expiry_notifications(
        self,
        session: AsyncSession,
        user_id: int,
        days_left: int,
    ) -> int:
        if days_left <= 0:
            return 0
        active_packages = await user_device_package_dal.get_active_packages(session, user_id)
        if not active_packages:
            return 0
        now = datetime.now(timezone.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        matching_packages = [
            package
            for package in active_packages
            if (now + timedelta(days=days_left - 1)) < package.expires_at <= (now + timedelta(days=days_left))
            and (not package.expiry_notified_at or package.expiry_notified_at < day_start)
        ]
        if not matching_packages:
            return 0

        db_user = await user_dal.get_user_by_id(session, user_id)
        lang = self.settings.DEFAULT_LANGUAGE
        if db_user and db_user.language_code:
            lang = db_user.language_code

        nearest_expiry = min(pkg.expires_at for pkg in matching_packages)
        if self.i18n:
            try:
                text = self.i18n.gettext(
                    lang,
                    "extra_devices_expiring_soon",
                    days_left=days_left,
                    end_date=nearest_expiry.strftime("%Y-%m-%d"),
                )
            except Exception:
                text = f"Your additional devices package expires in {days_left} day(s)."
        else:
            text = f"Your additional devices package expires in {days_left} day(s)."

        try:
            button_text = "📱 Продлить пакет устройств"
            if self.i18n:
                try:
                    button_text = self.i18n.gettext(lang, "buy_extra_devices_button")
                except Exception:
                    pass
            renewal_markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=button_text, callback_data="main_action:buy_extra_devices")]
                ]
            )
            await self.bot.send_message(user_id, text, reply_markup=renewal_markup)
        except Exception as e:
            logging.error(f"Failed to send addon package upcoming expiry notification to user {user_id}: {e}")
            return 0

        for package in matching_packages:
            package.expiry_notified_at = now
        await session.flush()
        return len(matching_packages)

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
