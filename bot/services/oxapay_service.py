import hmac
import hashlib
import json
import logging
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Dict, Any, Tuple

from aiohttp import ClientSession, ClientTimeout, web
from aiogram import Bot
from sqlalchemy.orm import sessionmaker

from config.settings import Settings
from bot.middlewares.i18n import JsonI18n
from bot.services.subscription_service import SubscriptionService
from bot.services.referral_service import ReferralService
from bot.services.device_package_service import DevicePackageService
from bot.services.squad_upgrade_service import SquadUpgradeService
from bot.services.notification_service import NotificationService
from bot.keyboards.inline.user_keyboards import get_connect_and_main_keyboard
from db.dal import payment_dal, user_dal
from bot.utils.text_sanitizer import sanitize_display_name, username_for_display


class OxaPayService:
    def __init__(
        self,
        *,
        bot: Bot,
        settings: Settings,
        i18n: JsonI18n,
        async_session_factory: sessionmaker,
        subscription_service: SubscriptionService,
        referral_service: ReferralService,
        device_package_service: Optional[DevicePackageService] = None,
        squad_upgrade_service: Optional[SquadUpgradeService] = None,
        default_return_url: str,
    ):
        self.bot = bot
        self.settings = settings
        self.i18n = i18n
        self.async_session_factory = async_session_factory
        self.subscription_service = subscription_service
        self.referral_service = referral_service
        self.device_package_service = device_package_service
        self.squad_upgrade_service = squad_upgrade_service

        self.base_url = (settings.OXAPAY_BASE_URL or "https://api.oxapay.com/v1").rstrip("/")
        self.api_key = settings.OXAPAY_MERCHANT_API_KEY or ""
        self.currency = (settings.OXAPAY_CURRENCY or "RUB").upper()
        self.lifetime_minutes = int(settings.OXAPAY_LIFETIME_MINUTES or 60)
        self.return_url = settings.OXAPAY_RETURN_URL or f"https://t.me/{default_return_url}"
        self.sandbox = bool(settings.OXAPAY_SANDBOX)

        self._timeout = ClientTimeout(total=20)
        self._session: Optional[ClientSession] = None

        self.configured: bool = bool(settings.OXAPAY_ENABLED and self.api_key)
        if not self.configured:
            logging.warning("OxaPayService initialized but not fully configured. Payments disabled.")

    async def _get_session(self) -> ClientSession:
        if self._session is None or self._session.closed:
            self._session = ClientSession(timeout=self._timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def create_invoice(
        self,
        *,
        payment_db_id: int,
        user_id: int,
        months: int,
        amount: float,
        description: str,
        currency: str | None = None,
    ) -> Tuple[bool, Dict[str, Any]]:
        if not self.configured:
            logging.error("OxaPayService is not configured. Cannot create invoice.")
            return False, {"message": "service_not_configured"}

        session = await self._get_session()
        url = f"{self.base_url}/payment/invoice"
        amount_value = float(Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

        payload: Dict[str, Any] = {
            "amount": amount_value,
            "currency": (currency or self.currency).upper(),
            "lifetime": max(15, min(2880, self.lifetime_minutes)),
            "callback_url": self.settings.oxapay_full_webhook_url,
            "return_url": self.return_url,
            "order_id": str(payment_db_id),
            "description": description,
            "sandbox": self.sandbox,
            "email": f"{user_id}@telegram.org",
        }
        payload = {k: v for k, v in payload.items() if v not in (None, "")}

        headers = {
            "merchant_api_key": self.api_key,
            "Content-Type": "application/json",
        }

        try:
            async with session.post(url, json=payload, headers=headers) as response:
                response_text = await response.text()
                try:
                    response_data = json.loads(response_text) if response_text else {}
                except json.JSONDecodeError:
                    logging.error("OxaPay create_invoice: invalid JSON response: %s", response_text)
                    return False, {"status": response.status, "message": "invalid_json", "raw": response_text}

                if response.status != 200 or int(response_data.get("status", 0)) != 200:
                    logging.error(
                        "OxaPay create_invoice: API returned error (status=%s, body=%s)",
                        response.status,
                        response_data,
                    )
                    return False, {"status": response.status, "message": response_data}

                return True, response_data.get("data") or response_data
        except Exception as exc:
            logging.error("OxaPay create_invoice: request failed: %s", exc, exc_info=True)
            return False, {"message": str(exc)}

    def _validate_signature(self, raw_body: bytes, header_signature: str) -> bool:
        if not self.api_key or not header_signature:
            return False
        digest = hmac.new(self.api_key.encode("utf-8"), raw_body, hashlib.sha512).hexdigest()
        return hmac.compare_digest(digest.lower(), header_signature.lower())

    async def webhook_route(self, request: web.Request) -> web.Response:
        if not self.configured:
            return web.Response(status=503, text="oxapay_disabled")

        raw_body = await request.read()
        try:
            payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except Exception:
            return web.Response(status=400, text="bad_request")

        if not isinstance(payload, dict):
            return web.Response(status=400, text="bad_request")

        hmac_header = request.headers.get("HMAC", "")
        if not self._validate_signature(raw_body, hmac_header):
            logging.error("OxaPay webhook: invalid HMAC signature")
            return web.Response(status=403, text="invalid_signature")

        payment_type = str(payload.get("type") or "").lower()
        if payment_type != "invoice":
            return web.Response(text="ok")

        status = str(payload.get("status") or "").lower()
        provider_payment_id = str(payload.get("track_id") or "").strip()
        order_id_raw = payload.get("order_id")

        payment_db_id: Optional[int] = None
        if isinstance(order_id_raw, int):
            payment_db_id = order_id_raw
        elif isinstance(order_id_raw, str) and order_id_raw.isdigit():
            payment_db_id = int(order_id_raw)

        async with self.async_session_factory() as session:
            payment = None
            if payment_db_id is not None:
                payment = await payment_dal.get_payment_by_db_id(session, payment_db_id)
            if not payment and provider_payment_id:
                payment = await payment_dal.get_payment_by_provider_payment_id(session, provider_payment_id)

            if not payment:
                logging.error("OxaPay webhook: payment not found (order_id=%s, track_id=%s)", order_id_raw, provider_payment_id)
                return web.Response(status=404, text="payment_not_found")

            payment_months = payment.subscription_duration_months or 1

            if status in {"paid", "manual_accept"}:
                if payment.status == "succeeded":
                    # Idempotency: repeated provider webhooks for already processed payment
                    return web.Response(text="ok")
                try:
                    await payment_dal.update_provider_payment_and_status(
                        session,
                        payment.payment_id,
                        provider_payment_id or str(payment.payment_id),
                        "succeeded",
                    )

                    if (payment.provider or "").endswith("-addon") and self.device_package_service:
                        package_key = (payment.description or "").split(" ")[2] if payment.description else ""
                        activation = {"end_date": await self.device_package_service.activate_paid_package(
                            session,
                            user_id=payment.user_id,
                            package_key=package_key,
                            months=payment_months,
                            provider="oxapay-addon",
                            payment_id=payment.payment_id,
                        )}
                        referral_bonus = None
                    elif (payment.provider or "").endswith("-upgrade") and self.squad_upgrade_service:
                        activation = {"end_date": await self.squad_upgrade_service.activate_paid_upgrade(
                            session,
                            user_id=payment.user_id,
                            duration_days=payment_months * 30,
                            provider="oxapay-upgrade",
                            payment_id=payment.payment_id,
                        )}
                        referral_bonus = None
                    else:
                        activation = await self.subscription_service.activate_subscription(
                            session, payment.user_id, payment_months, float(payment.amount), payment.payment_id, provider="oxapay"
                        )
                        referral_bonus = await self.referral_service.apply_referral_bonuses_for_payment(
                            session, payment.user_id, payment_months, current_payment_db_id=payment.payment_id, skip_if_active_before_payment=False
                        )

                    await session.commit()
                except Exception as exc:
                    await session.rollback()
                    logging.error("OxaPay webhook: failed to process payment %s: %s", provider_payment_id, exc, exc_info=True)
                    return web.Response(status=500, text="processing_error")

                db_user = payment.user or await user_dal.get_user_by_id(session, payment.user_id)
                lang = db_user.language_code if db_user and db_user.language_code else self.settings.DEFAULT_LANGUAGE
                _ = lambda k, **kw: self.i18n.gettext(lang, k, **kw) if self.i18n else k

                config_link = (
                    activation.get("subscription_url") if activation else None
                ) or _("config_link_not_available")
                final_end = activation.get("end_date") if activation else None
                applied_days = 0
                applied_promo_days = activation.get("applied_promo_bonus_days", 0) if activation else 0

                if referral_bonus and referral_bonus.get("referee_new_end_date"):
                    final_end = referral_bonus["referee_new_end_date"]
                    applied_days = referral_bonus.get("referee_bonus_applied_days", 0)

                if (payment.provider or "").endswith("-addon"):
                    text = _("extra_devices_purchase_success", end_date=final_end.strftime("%Y-%m-%d") if final_end else "")
                elif (payment.provider or "").endswith("-upgrade"):
                    text = _("squad_upgrade_purchase_success", end_date=final_end.strftime("%Y-%m-%d") if final_end else "")
                elif applied_days:
                    inviter_name_display = _("friend_placeholder")
                    if db_user and db_user.referred_by_id:
                        inviter = await user_dal.get_user_by_id(session, db_user.referred_by_id)
                        if inviter:
                            safe_name = sanitize_display_name(inviter.first_name) if inviter.first_name else None
                            if safe_name:
                                inviter_name_display = safe_name
                            elif inviter.username:
                                inviter_name_display = username_for_display(inviter.username, with_at=False)

                    text = _(
                        "payment_successful_with_referral_bonus_full",
                        months=payment_months,
                        base_end_date=activation["end_date"].strftime("%Y-%m-%d") if activation and activation.get("end_date") else final_end.strftime("%Y-%m-%d") if final_end else "",
                        bonus_days=applied_days,
                        final_end_date=final_end.strftime("%Y-%m-%d") if final_end else "",
                        inviter_name=inviter_name_display,
                        config_link=config_link,
                    )
                elif applied_promo_days and final_end:
                    text = _(
                        "payment_successful_with_promo_full",
                        months=payment_months,
                        bonus_days=applied_promo_days,
                        end_date=final_end.strftime("%Y-%m-%d"),
                        config_link=config_link,
                    )
                else:
                    text = _(
                        "payment_successful_full",
                        months=payment_months,
                        end_date=final_end.strftime("%Y-%m-%d") if final_end else "",
                        config_link=config_link,
                    )

                markup = get_connect_and_main_keyboard(
                    lang,
                    self.i18n,
                    self.settings,
                    config_link,
                    preserve_message=True,
                )
                try:
                    await self.bot.send_message(
                        payment.user_id,
                        text,
                        reply_markup=markup,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                except Exception as exc:
                    logging.error("OxaPay webhook: failed to notify user %s: %s", payment.user_id, exc)

                try:
                    notification_service = NotificationService(self.bot, self.settings, self.i18n)
                    await notification_service.notify_payment_received(
                        user_id=payment.user_id,
                        amount=float(payment.amount),
                        currency=payment.currency,
                        months=payment_months,
                        payment_provider="oxapay",
                        username=db_user.username if db_user else None,
                    )
                except Exception as exc:
                    logging.error("OxaPay webhook: failed to notify admins: %s", exc)

                return web.Response(text="ok")

            if status in {"expired", "underpaid", "refunded", "refunding"}:
                try:
                    await payment_dal.update_provider_payment_and_status(
                        session,
                        payment.payment_id,
                        provider_payment_id or str(payment.payment_id),
                        "canceled",
                    )
                    await session.commit()
                except Exception as exc:
                    await session.rollback()
                    logging.error("OxaPay webhook: failed to cancel payment %s: %s", provider_payment_id, exc)
                    return web.Response(status=500, text="processing_error")

                db_user = payment.user or await user_dal.get_user_by_id(session, payment.user_id)
                lang = db_user.language_code if db_user and db_user.language_code else self.settings.DEFAULT_LANGUAGE
                _ = lambda k, **kw: self.i18n.gettext(lang, k, **kw) if self.i18n else k
                try:
                    await self.bot.send_message(payment.user_id, _("payment_failed"))
                except Exception:
                    pass
                return web.Response(text="ok")

            return web.Response(text="ok")


async def oxapay_webhook_route(request: web.Request) -> web.Response:
    service: OxaPayService = request.app["oxapay_service"]
    return await service.webhook_route(request)
