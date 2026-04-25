import logging
import hmac
import hashlib
import json
from typing import Optional

from aiohttp import web
from aiogram import Bot
from sqlalchemy.orm import sessionmaker

from config.settings import Settings
from bot.middlewares.i18n import JsonI18n
from bot.services.subscription_service import SubscriptionService
from bot.services.panel_api_service import PanelApiService
from bot.services.referral_service import ReferralService
from bot.services.device_package_service import DevicePackageService
from bot.services.squad_upgrade_service import SquadUpgradeService
from .notification_service import NotificationService
from bot.keyboards.inline.user_keyboards import get_connect_and_main_keyboard
from db.dal import payment_dal, user_dal, subscription_dal
from bot.utils.text_sanitizer import sanitize_display_name, username_for_display


def convert_period_to_months(period: Optional[str]) -> int:
    """Map Tribute subscription period strings to months."""
    if not period:
        return 1

    mapping = {
        "monthly": 1,
        "quarterly": 3,
        "3-month": 3,
        "3months": 3,
        "3-months": 3,
        "q": 3,
        "halfyearly": 6,
        "yearly": 12,
        "annual": 12,
        "y": 12,
    }
    return mapping.get(period.lower(), 1)


class TributeService:
    def __init__(
        self,
        bot: Bot,
        settings: Settings,
        i18n: JsonI18n,
        async_session_factory: sessionmaker,
        panel_service: PanelApiService,
        subscription_service: SubscriptionService,
        referral_service: ReferralService,
        device_package_service: DevicePackageService,
        squad_upgrade_service: SquadUpgradeService,
    ):
        self.bot = bot
        self.settings = settings
        self.i18n = i18n
        self.async_session_factory = async_session_factory
        self.panel_service = panel_service
        self.subscription_service = subscription_service
        self.referral_service = referral_service
        self.device_package_service = device_package_service
        self.squad_upgrade_service = squad_upgrade_service

    async def handle_webhook(self, raw_body: bytes, signature_header: Optional[str]) -> web.Response:
        settings = self.settings
        bot = self.bot
        i18n = self.i18n
        async_session_factory = self.async_session_factory
        subscription_service = self.subscription_service
        referral_service = self.referral_service
        device_package_service = self.device_package_service
        squad_upgrade_service = self.squad_upgrade_service

        def ok(data: Optional[dict] = None) -> web.Response:
            payload = {"status": "ok"}
            if data:
                payload.update(data)
            return web.json_response(payload, status=200)

        def ignored(reason: str) -> web.Response:
            return web.json_response({"status": "ignored", "reason": reason}, status=200)

        def bad_request(reason: str) -> web.Response:
            return web.json_response({"status": "error", "reason": reason}, status=400)

        if settings.TRIBUTE_API_KEY:
            if not signature_header:
                return web.json_response({"status": "error", "reason": "no_signature"}, status=403)
            expected_sig = hmac.new(settings.TRIBUTE_API_KEY.encode(), raw_body,
                                    hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected_sig, signature_header):
                return web.json_response({"status": "error", "reason": "invalid_signature"}, status=403)

        try:
            payload = json.loads(raw_body.decode())
        except Exception:
            return bad_request("invalid_json")

        logging.info(
            "Tribute webhook data: %s",
            json.dumps(payload, ensure_ascii=False),
        )

        # Tribute subscription webhook events:
        # name: new_subscription | renewed_subscription | cancelled_subscription
        event_name = payload.get("name")
        data = payload.get("payload", {})

        # Mandatory routing fields
        user_id = data.get("telegram_user_id")
        if not user_id:
            # Permanent format issue — acknowledge to avoid retries
            return ignored("missing_telegram_user_id")

        period_val = data.get("period")
        months = convert_period_to_months(period_val)

        # Tribute sends amount in minor units (kopecks/cents). Convert to major units before persisting.
        amount_value = data.get("amount") or data.get("price")
        currency = (data.get("currency") or settings.DEFAULT_CURRENCY_SYMBOL or "RUB").upper()
        if amount_value is not None:
            try:
                amount_minor_units = float(amount_value)
            except (TypeError, ValueError):
                amount_minor_units = 0.0
            amount_float = round(amount_minor_units / 100.0, 2)
        else:
            amount_float = 0.0

        async with async_session_factory() as session:
            if event_name in {"new_subscription", "renewed_subscription"}:
                addon_package_key = self._extract_addon_package_key(data)
                if addon_package_key:
                    addon_months = self._extract_addon_months(data, fallback=months)
                    await self._handle_tribute_paid_addon_event(
                        session=session,
                        raw_body=raw_body,
                        user_id=int(user_id),
                        months=addon_months,
                        amount_float=float(amount_float),
                        currency=currency,
                        event_name=event_name,
                        package_key=addon_package_key,
                        bot=bot,
                        i18n=i18n,
                        settings=settings,
                        device_package_service=device_package_service,
                    )
                elif self._is_squad_upgrade_payment(data):
                    await self._handle_tribute_paid_squad_upgrade_event(
                        session=session,
                        raw_body=raw_body,
                        user_id=int(user_id),
                        months=months,
                        amount_float=float(amount_float),
                        currency=currency,
                        event_name=event_name,
                        bot=bot,
                        i18n=i18n,
                        settings=settings,
                        squad_upgrade_service=squad_upgrade_service,
                    )
                else:
                    await self._handle_tribute_paid_subscription_event(
                        session=session,
                        raw_body=raw_body,
                        user_id=int(user_id),
                        months=months,
                        amount_float=float(amount_float),
                        currency=currency,
                        event_name=event_name,
                        bot=bot,
                        i18n=i18n,
                        settings=settings,
                        subscription_service=subscription_service,
                        referral_service=referral_service,
                    )
            elif event_name == "cancelled_subscription":
                await self._handle_tribute_cancellation(session, int(user_id), bot, i18n)
                
            else:
                await session.commit()
        # Acknowledge to Tribute that webhook was received and processed/accepted
        return ok({"event": event_name or "unknown"})

    def _extract_addon_package_key(self, data: dict) -> Optional[str]:
        subscription_name = str(data.get("subscription_name") or "").strip().lower()

        # Preferred explicit mapping by Tribute subscription_name marker from ENV
        for package_key, package_cfg in self.settings.addon_device_packages.items():
            configured_name = package_cfg.get("tribute_subscription_name")
            if configured_name:
                marker = str(configured_name).strip().lower()
                if marker and marker in subscription_name:
                    return str(package_key)

        direct_candidates = (
            data.get("addon_package_key"),
            data.get("device_package_key"),
        )
        for value in direct_candidates:
            value_str = str(value) if value is not None else None
            if value_str in {"1", "2", "3"}:
                return value_str

        metadata_candidates = (
            data.get("metadata"),
            data.get("meta"),
            data.get("custom_fields"),
            data.get("custom"),
        )
        addon_scope_markers = {
            "addon",
            "addon_devices",
            "device_package",
            "devices_addon",
        }
        for candidate in metadata_candidates:
            parsed = candidate
            if isinstance(candidate, str):
                try:
                    parsed = json.loads(candidate)
                except Exception:
                    parsed = None
            if isinstance(parsed, dict):
                for key in ("addon_package_key", "device_package_key"):
                    value = parsed.get(key)
                    value_str = str(value) if value is not None else None
                    if value_str in {"1", "2", "3"}:
                        return value_str
                scope_values = [
                    parsed.get("payment_target"),
                    parsed.get("target"),
                    parsed.get("scope"),
                    parsed.get("product_type"),
                    parsed.get("subscription_type"),
                ]
                is_addon_scope = any(
                    (str(v).strip().lower() in addon_scope_markers)
                    for v in scope_values
                    if v is not None
                )
                if is_addon_scope:
                    generic = parsed.get("package_key")
                    generic_str = str(generic) if generic is not None else None
                    if generic_str in {"1", "2", "3"}:
                        return generic_str
        return None

    def _extract_addon_months(self, data: dict, fallback: int = 1) -> int:
        direct_candidates = (
            data.get("months"),
            data.get("duration_months"),
            data.get("period_months"),
        )
        for value in direct_candidates:
            try:
                parsed = int(value)
                if parsed > 0:
                    return parsed
            except (TypeError, ValueError):
                pass

        period = data.get("period")
        if isinstance(period, str):
            return convert_period_to_months(period)

        return fallback if fallback > 0 else 1

    def _is_squad_upgrade_payment(self, data: dict) -> bool:
        offer = self.settings.squad_upgrade_offer or {}
        configured_name = str(offer.get("tribute_subscription_name") or "").strip().lower()
        subscription_name = str(data.get("subscription_name") or "").strip().lower()
        if configured_name and configured_name in subscription_name:
            return True

        metadata_candidates = (
            data.get("metadata"),
            data.get("meta"),
            data.get("custom_fields"),
            data.get("custom"),
        )
        for candidate in metadata_candidates:
            parsed = candidate
            if isinstance(candidate, str):
                try:
                    parsed = json.loads(candidate)
                except Exception:
                    parsed = None
            if isinstance(parsed, dict):
                target = str(parsed.get("payment_target") or parsed.get("target") or "").strip().lower()
                if target in {"squad_upgrade", "upgrade"}:
                    return True
        return False

    async def _handle_tribute_paid_addon_event(
        self,
        session,
        raw_body: bytes,
        user_id: int,
        months: int,
        amount_float: float,
        currency: str,
        event_name: str,
        package_key: str,
        bot: Bot,
        i18n: JsonI18n,
        settings: Settings,
        device_package_service: DevicePackageService,
    ) -> None:
        data = json.loads(raw_body.decode()).get("payload", {})
        candidate_event_id = str(
            data.get("event_id")
            or data.get("payment_id")
            or data.get("purchase_id")
            or data.get("invoice_id")
            or ""
        )
        if candidate_event_id:
            provider_payment_id = f"tribute_addon:{candidate_event_id}"
        else:
            payload_hash = hashlib.sha256(raw_body).hexdigest()[:16]
            provider_payment_id = f"tribute_addon:{package_key}:{payload_hash}"

        payment_record, created_new_payment = await payment_dal.ensure_payment_with_provider_id(
            session,
            user_id=user_id,
            amount=amount_float,
            currency=currency,
            months=months,
            description=f"Tribute addon package {package_key} ({event_name})",
            provider="tribute",
            provider_payment_id=provider_payment_id,
            return_created=True,
        )
        if not created_new_payment:
            await session.commit()
            return

        expires_at = await device_package_service.activate_paid_package(
            session,
            user_id=user_id,
            package_key=package_key,
            months=months,
            provider="tribute",
            payment_id=payment_record.payment_id,
        )
        await session.commit()
        if not expires_at:
            return

        db_user = await user_dal.get_user_by_id(session, user_id)
        lang = db_user.language_code if db_user and db_user.language_code else settings.DEFAULT_LANGUAGE
        _ = lambda k, **kw: i18n.gettext(lang, k, **kw)
        try:
            await bot.send_message(
                user_id,
                _("extra_devices_purchase_success", end_date=expires_at.strftime('%Y-%m-%d')),
            )
        except Exception as e:
            logging.error(f"Failed to send Tribute addon success message to user {user_id}: {e}")
        try:
            notification_service = NotificationService(bot, settings, i18n)
            user = await user_dal.get_user_by_id(session, user_id)
            await notification_service.notify_payment_received(
                user_id=user_id,
                amount=amount_float,
                currency=currency,
                months=months,
                payment_provider="tribute-addon",
                username=user.username if user else None,
            )
        except Exception as e:
            logging.error(f"Failed to send tribute addon payment notification: {e}")

    async def _handle_tribute_paid_squad_upgrade_event(
        self,
        session,
        raw_body: bytes,
        user_id: int,
        months: int,
        amount_float: float,
        currency: str,
        event_name: str,
        bot: Bot,
        i18n: JsonI18n,
        settings: Settings,
        squad_upgrade_service: SquadUpgradeService,
    ) -> None:
        data = json.loads(raw_body.decode()).get("payload", {})
        candidate_event_id = str(
            data.get("event_id")
            or data.get("payment_id")
            or data.get("purchase_id")
            or data.get("invoice_id")
            or ""
        )
        provider_payment_id = (
            f"tribute_upgrade:{candidate_event_id}"
            if candidate_event_id
            else f"tribute_upgrade:{hashlib.sha256(raw_body).hexdigest()[:16]}"
        )
        payment_record, created_new_payment = await payment_dal.ensure_payment_with_provider_id(
            session,
            user_id=user_id,
            amount=amount_float,
            currency=currency,
            months=months,
            description=f"Tribute squad upgrade ({event_name})",
            provider="tribute",
            provider_payment_id=provider_payment_id,
            return_created=True,
        )
        if not created_new_payment:
            await session.commit()
            return
        expires_at = await squad_upgrade_service.activate_paid_upgrade(
            session,
            user_id=user_id,
            months=months,
            provider="tribute",
            payment_id=payment_record.payment_id,
        )
        await session.commit()
        if not expires_at:
            return
        db_user = await user_dal.get_user_by_id(session, user_id)
        lang = db_user.language_code if db_user and db_user.language_code else settings.DEFAULT_LANGUAGE
        _ = lambda k, **kw: i18n.gettext(lang, k, **kw)
        try:
            await bot.send_message(
                user_id,
                _("squad_upgrade_purchase_success", end_date=expires_at.strftime('%Y-%m-%d')),
            )
        except Exception as e:
            logging.error(f"Failed to send Tribute squad upgrade success message to user {user_id}: {e}")
        try:
            notification_service = NotificationService(bot, settings, i18n)
            user = await user_dal.get_user_by_id(session, user_id)
            await notification_service.notify_payment_received(
                user_id=user_id,
                amount=amount_float,
                currency=currency,
                months=months,
                payment_provider="tribute-upgrade",
                username=user.username if user else None,
            )
        except Exception as e:
            logging.error(f"Failed to send tribute squad upgrade payment notification: {e}")

    async def _handle_tribute_paid_subscription_event(
        self,
        session,
        raw_body: bytes,
        user_id: int,
        months: int,
        amount_float: float,
        currency: str,
        event_name: str,
        bot: Bot,
        i18n: JsonI18n,
        settings: Settings,
        subscription_service: SubscriptionService,
        referral_service: ReferralService,
    ) -> None:
        """Handle new/renewed Tribute subscription events in an idempotent way."""
        data = json.loads(raw_body.decode()).get("payload", {})
        candidate_event_id = str(
            data.get("event_id")
            or data.get("payment_id")
            or data.get("purchase_id")
            or data.get("invoice_id")
            or ""
        )
        if candidate_event_id:
            provider_payment_id = candidate_event_id
        else:
            sub_id_part = str(data.get("subscription_id") or "sub")
            payload_hash = hashlib.sha256(raw_body).hexdigest()[:16]
            provider_payment_id = f"{sub_id_part}:{payload_hash}"

        payment_record, created_new_payment = await payment_dal.ensure_payment_with_provider_id(
            session,
            user_id=user_id,
            amount=amount_float,
            currency=currency,
            months=months,
            description=f"Tribute subscription ({event_name})",
            provider="tribute",
            provider_payment_id=provider_payment_id,
            return_created=True,
        )

        if not created_new_payment:
            logging.info(
                "Tribute webhook duplicate detected (provider_payment_id=%s, event=%s, user_id=%s). "
                "Skipping activation/bonuses/notifications.",
                provider_payment_id,
                event_name,
                user_id,
            )
            await session.commit()
            return

        activation_details = await subscription_service.activate_subscription(
            session,
            user_id,
            months,
            amount_float,
            payment_record.payment_id,
            provider="tribute",
        )
        referral_bonus = await referral_service.apply_referral_bonuses_for_payment(
            session,
            user_id,
            months,
            current_payment_db_id=payment_record.payment_id,
            skip_if_active_before_payment=False,
        )
        await session.commit()

        db_user = await user_dal.get_user_by_id(session, user_id)
        lang = db_user.language_code if db_user and db_user.language_code else settings.DEFAULT_LANGUAGE
        _ = lambda k, **kw: i18n.gettext(lang, k, **kw)

        applied_ref_days = referral_bonus.get('referee_bonus_applied_days') if referral_bonus else None
        final_end = (referral_bonus.get('referee_new_end_date')
                     if referral_bonus else None)
        if not final_end:
            final_end = activation_details.get('end_date')

        if final_end:
            config_link = activation_details.get("subscription_url") or _(
                "config_link_not_available"
            )

            if applied_ref_days:
                inviter_name_display = _('friend_placeholder')
                if db_user and db_user.referred_by_id:
                    inviter = await user_dal.get_user_by_id(session, db_user.referred_by_id)
                    if inviter:
                        safe_name = sanitize_display_name(inviter.first_name) if inviter.first_name else None
                        if safe_name:
                            inviter_name_display = safe_name
                        elif inviter.username:
                            inviter_name_display = username_for_display(inviter.username, with_at=False)
                success_msg = _(
                    "payment_successful_with_referral_bonus_full",
                    months=months,
                    base_end_date=activation_details["end_date"].strftime('%Y-%m-%d'),
                    bonus_days=applied_ref_days,
                    final_end_date=final_end.strftime('%Y-%m-%d'),
                    inviter_name=inviter_name_display,
                    config_link=config_link,
                )
            else:
                success_msg = _(
                    "payment_successful_full",
                    months=months,
                    end_date=final_end.strftime('%Y-%m-%d'),
                    config_link=config_link,
                )
            markup = get_connect_and_main_keyboard(
                lang,
                i18n,
                settings,
                config_link,
                preserve_message=True,
            )

            try:
                await bot.send_message(
                    user_id,
                    success_msg,
                    reply_markup=markup,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception as e:
                logging.error(
                    f"Failed to send Tribute payment success message to user {user_id}: {e}")

        try:
            notification_service = NotificationService(bot, settings, i18n)
            user = await user_dal.get_user_by_id(session, user_id)
            await notification_service.notify_payment_received(
                user_id=user_id,
                amount=amount_float,
                currency=currency,
                months=months,
                payment_provider="tribute",
                username=user.username if user else None
            )
        except Exception as e:
            logging.error(f"Failed to send tribute payment notification: {e}")

    async def _handle_tribute_cancellation(self, session, user_id: int, bot: Bot, i18n: JsonI18n):
        """Handle Tribute cancellation without changing subscription end date.

        Renewal is now performed only by explicit Tribute renewal webhooks.
        """
        from db.dal import subscription_dal, user_dal
        from bot.keyboards.inline.user_keyboards import get_subscribe_only_markup

        try:
            active_subscriptions = await subscription_dal.get_active_subscriptions_for_user(session, user_id)
            tribute_subscriptions = [sub for sub in active_subscriptions if getattr(sub, "provider", None) == "tribute"]

            for sub in tribute_subscriptions:
                await subscription_dal.update_subscription(
                    session,
                    sub.subscription_id,
                    {
                        "status_from_panel": "CANCELLED",
                        "skip_notifications": True,
                    },
                )

            if not tribute_subscriptions:
                logging.info(
                    "Tribute cancellation received for user %s but no active Tribute subscriptions were found.",
                    user_id,
                )

            await session.commit()

            # Send notification about cancellation if enabled
            if not self.settings.TRIBUTE_SKIP_CANCELLATION_NOTIFICATIONS:
                db_user = await user_dal.get_user_by_id(session, user_id)
                lang = db_user.language_code if db_user and db_user.language_code else self.settings.DEFAULT_LANGUAGE
                first_name = db_user.first_name or f"User {user_id}" if db_user else f"User {user_id}"
                
                _ = lambda k, **kw: i18n.gettext(lang, k, **kw) if i18n else k
                markup = get_subscribe_only_markup(lang, i18n)
                
                cancellation_msg = _(
                    "tribute_subscription_cancelled",
                    default="🚨 <b>Подписка отменена</b>\n\n"
                           "Ваша подписка Tribute была отменена и не будет продлеваться автоматически без нового webhook о продлении.\n\n"
                           "Для продления подписки нажмите кнопку ниже.",
                    user_name=first_name
                )
                
                try:
                    await bot.send_message(
                        int(user_id),
                        cancellation_msg,
                        reply_markup=markup,
                        parse_mode="HTML"
                    )
                except Exception as e:
                    logging.error(f"Failed to send tribute cancellation notification to user {user_id}: {e}")
                    
            logging.info(f"Tribute subscription cancelled for user {user_id} (end date unchanged)")
            
        except Exception as e:
            logging.error(f"Error handling tribute cancellation for user {user_id}: {e}")
            await session.rollback()


async def tribute_webhook_route(request: web.Request):
    """AIOHTTP route handler for Tribute webhook calls."""
    tribute_service: TributeService = request.app['tribute_service']
    raw_body = await request.read()
    signature_header = request.headers.get('trbt-signature')
    return await tribute_service.handle_webhook(raw_body, signature_header)
