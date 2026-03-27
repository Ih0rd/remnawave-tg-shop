import logging
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional, Tuple, Dict
from aiogram import Bot

from config.settings import Settings

from db.dal import promo_code_dal, user_dal
from db.models import PromoCode, User

from .subscription_service import SubscriptionService
from .device_package_service import DevicePackageService
from .squad_upgrade_service import SquadUpgradeService
from bot.middlewares.i18n import JsonI18n
from .notification_service import NotificationService


class PromoCodeService:

    def __init__(self, settings: Settings,
                 subscription_service: SubscriptionService, bot: Bot,
                 i18n: JsonI18n,
                 device_package_service: DevicePackageService | None = None,
                 squad_upgrade_service: SquadUpgradeService | None = None):
        self.settings = settings
        self.subscription_service = subscription_service
        self.bot = bot
        self.i18n = i18n
        self.device_package_service = device_package_service
        self.squad_upgrade_service = squad_upgrade_service

    async def apply_promo_code(
        self,
        session: AsyncSession,
        user_id: int,
        code_input: str,
        user_lang: str,
    ) -> Tuple[bool, datetime | str]:
        _ = lambda k, **kw: self.i18n.gettext(user_lang, k, **kw)
        code_input_upper = code_input.strip().upper()

        promo_data = await promo_code_dal.get_active_promo_code_by_code_str(
            session, code_input_upper)

        if not promo_data:
            return False, _("promo_code_not_found", code=code_input_upper)

        existing_activation = await promo_code_dal.get_user_activation_for_promo(
            session, promo_data.promo_code_id, user_id)
        if existing_activation:
            return False, _("promo_code_already_used_by_user",
                            code=code_input_upper)

        promo_type = (promo_data.promo_type or "bonus_days").lower()
        bonus_days = promo_data.bonus_days or 0
        upgrade_days = promo_data.upgrade_days or 0
        package_key = (promo_data.package_key or "").strip()

        new_end_date: datetime | None = None
        promo_result: datetime | str | None = None
        if promo_type == "device_package":
            if not self.device_package_service or not package_key:
                logging.error(
                    "Promo code %s has invalid package config: device_package_service=%s package_key=%s",
                    code_input_upper,
                    bool(self.device_package_service),
                    package_key,
                )
                return False, _("error_applying_promo_bonus")

            package_expires_at = await self.device_package_service.activate_paid_package(
                session,
                user_id=user_id,
                package_key=package_key,
                months=1,
                provider="promo_code",
                payment_id=None,
            )
            if package_expires_at:
                promo_result = _("promo_code_applied_success_package", package_key=package_key, expires_at=package_expires_at.strftime("%d.%m.%Y %H:%M:%S"))
            else:
                promo_result = None
        elif promo_type == "squad_upgrade":
            if not self.squad_upgrade_service:
                return False, _("error_applying_promo_bonus")
            duration_days = upgrade_days if upgrade_days > 0 else None
            upgrade_expires_at = await self.squad_upgrade_service.activate_paid_upgrade(
                session,
                user_id=user_id,
                months=1,
                duration_days=duration_days,
                provider="promo_code",
                payment_id=None,
            )
            if upgrade_expires_at:
                promo_result = _("promo_code_applied_success_upgrade", expires_at=upgrade_expires_at.strftime("%d.%m.%Y %H:%M:%S"))
            else:
                promo_result = None
        else:
            new_end_date = await self.subscription_service.extend_active_subscription_days(
                session=session,
                user_id=user_id,
                bonus_days=bonus_days,
                reason=f"promo code {code_input_upper}",
            )
            promo_result = new_end_date

        if promo_result:
            activation_recorded = await promo_code_dal.record_promo_activation(
                session, promo_data.promo_code_id, user_id, payment_id=None)
            promo_incremented = await promo_code_dal.increment_promo_code_usage(
                session, promo_data.promo_code_id)

            if activation_recorded and promo_incremented:
                # Send notification about promo activation
                try:
                    notification_service = NotificationService(self.bot, self.settings, self.i18n)
                    user = await user_dal.get_user_by_id(session, user_id)
                    await notification_service.notify_promo_activation(
                        user_id=user_id,
                        promo_code=code_input_upper,
                        bonus_days=bonus_days,
                        username=user.username if user else None
                    )
                except Exception as e:
                    logging.error(f"Failed to send promo activation notification: {e}")
                
                return True, promo_result
            else:

                logging.error(
                    f"Failed to record activation or increment usage for promo {promo_data.code} by user {user_id}"
                )
                return False, _("error_applying_promo_bonus")
        else:
            return False, _("error_applying_promo_bonus")
