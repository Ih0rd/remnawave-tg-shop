import json
import logging
import hmac
import hashlib
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
from bot.keyboards.inline.user_keyboards import get_connect_and_main_keyboard
from bot.services.notification_service import NotificationService
from db.dal import payment_dal, user_dal
from bot.utils.text_sanitizer import sanitize_display_name, username_for_display


class RollyPayService:
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

        self.base_url = (settings.ROLLYPAY_BASE_URL or "https://rollypay.io").rstrip("/")
        self.api_key = settings.ROLLYPAY_API_KEY
        self.signing_secret = settings.ROLLYPAY_SIGNING_SECRET
        payment_method = (settings.ROLLYPAY_PAYMENT_METHOD or "").strip().lower()
        self.payment_method = payment_method if payment_method in {"sbp", "card"} else None
        self.return_url = settings.ROLLYPAY_RETURN_URL or f"https://t.me/{default_return_url}"
        self.failed_url = settings.ROLLYPAY_FAILED_URL or self.return_url

        self._timeout = ClientTimeout(total=20)
        self._session: Optional[ClientSession] = None
        self._auth_headers = {"Content-Type": "application/json"}
        self.configured: bool = bool(
            settings.ROLLYPAY_ENABLED and self.api_key
        )
        if not self.configured:
            logging.warning("RollyPayService initialized but not fully configured. Payments disabled.")

    async def _get_session(self) -> ClientSession:
        if self._session is None or self._session.closed:
            self._session = ClientSession(timeout=self._timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def create_transaction(
        self,
        *,
        payment_db_id: int,
        user_id: int,
        months: int,
        amount: float,
        currency: Optional[str],
        description: str,
        payload: Optional[str] = None,
    ) -> Tuple[bool, Dict[str, Any]]:
        if not self.configured:
            logging.error("RollyPayService is not configured. Cannot create transaction.")
            return False, {"message": "service_not_configured"}

        session = await self._get_session()
        url = f"{self.base_url}/api/v1/payments"
        currency_code = (currency or self.settings.DEFAULT_CURRENCY_SYMBOL or "RUB").upper()

        body: Dict[str, Any] = {
            "amount": float(amount),
            "currency": currency_code,
            "description": description,
            "order_id": str(payment_db_id),
            "callback_url": self.settings.rollypay_full_webhook_url,
            "success_url": self.return_url,
            "fail_url": self.failed_url,
            "metadata": {"payload": payload} if payload else None,
            "payment_method": self.payment_method,
        }

        # Remove optional keys with falsy values to avoid validation errors
        clean_body = {k: v for k, v in body.items() if v not in (None, "")}

        try:
            headers={**self._auth_headers, "X-API-Key": self.api_key or "", "X-Nonce": str(__import__("time").time_ns())}
            async with session.post(url, json=clean_body, headers=headers) as response:
                response_text = await response.text()
                try:
                    response_data = json.loads(response_text) if response_text else {}
                except json.JSONDecodeError:
                    logging.error("RollyPay create_transaction: invalid JSON response: %s", response_text)
                    return False, {
                        "status": response.status,
                        "message": "invalid_json",
                        "raw": response_text,
                    }

                if response.status not in (200,201):
                    logging.error(
                        "RollyPay create_transaction: API returned error (status=%s, body=%s)",
                        response.status,
                        response_data,
                    )
                    return False, {"status": response.status, "message": response_data}

                return True, response_data
        except Exception as exc:
            logging.error("RollyPay create_transaction: request failed: %s", exc, exc_info=True)
            return False, {"message": str(exc)}

    async def webhook_route(self, request: web.Request) -> web.Response:
        if not self.configured:
            return web.Response(status=503, text="rollypay_disabled")

        try:
            raw_body = await request.text()
            data = json.loads(raw_body) if raw_body else {}
        except Exception as exc:
            logging.error("RollyPay webhook: failed to parse JSON: %s", exc)
            return web.Response(status=400, text="bad_request")

        if self.signing_secret:
            signature = request.headers.get("X-Signature", "")
            expected_signature = hmac.new(
                self.signing_secret.encode("utf-8"),
                raw_body.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            if not signature or not hmac.compare_digest(signature, expected_signature):
                logging.error("RollyPay webhook: invalid signature")
                return web.Response(status=403, text="forbidden")

        event = str(data.get("event") or "")
        obj = data.get("data") if isinstance(data.get("data"), dict) else data
        transaction_id = str(obj.get("payment_id") or obj.get("id") or "").strip()
        status = str(obj.get("status") or "").upper()
        amount_raw = obj.get("amount")
        currency = data.get("currency") or self.settings.DEFAULT_CURRENCY_SYMBOL or "RUB"

        if not transaction_id or not status:
            logging.error("RollyPay webhook: missing transaction id or status in payload: %s", data)
            return web.Response(status=400, text="missing_fields")

        async with self.async_session_factory() as session:
            payment = await payment_dal.get_payment_by_provider_payment_id(session, transaction_id)
            if not payment:
                logging.error("RollyPay webhook: payment not found for transaction %s", transaction_id)
                return web.Response(status=404, text="payment_not_found")

            if payment.status == "succeeded" and status in ("PAID","CONFIRMED","SUCCEEDED"):
                return web.Response(text="ok")

            payment_months = payment.subscription_duration_months or 1

            if status in ("PAID","CONFIRMED","SUCCEEDED") or event=="payment.paid":
                if amount_raw is not None:
                    try:
                        incoming_amount = Decimal(str(amount_raw)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                        expected_amount = Decimal(str(payment.amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                        if incoming_amount != expected_amount:
                            logging.warning(
                                "RollyPay webhook: amount mismatch for payment %s (expected %s, got %s)",
                                payment.payment_id,
                                expected_amount,
                                incoming_amount,
                            )
                    except Exception as exc:
                        logging.warning("RollyPay webhook: failed to compare amounts for %s: %s", payment.payment_id, exc)

                try:
                    await payment_dal.update_provider_payment_and_status(
                        session,
                        payment.payment_id,
                        transaction_id,
                        "succeeded",
                    )

                    if (payment.provider or "").endswith("-addon") and self.device_package_service:
                        package_key = (payment.description or "").split(" ")[2] if payment.description else ""
                        activation = {"end_date": await self.device_package_service.activate_paid_package(
                            session, payment.user_id, payment.payment_id, package_key=package_key, months=payment_months
                        )}
                        referral_bonus = None
                    elif (payment.provider or "").endswith("-upgrade") and self.squad_upgrade_service:
                        activation = {"end_date": await self.squad_upgrade_service.activate_paid_upgrade(
                            session, payment.user_id, payment.payment_id, duration_days=payment_months * 30
                        )}
                        referral_bonus = None
                    else:
                        activation = await self.subscription_service.activate_subscription(
                            session, payment.user_id, payment_months, float(payment.amount), payment.payment_id, provider="rollypay"
                        )
                        referral_bonus = await self.referral_service.apply_referral_bonuses_for_payment(
                            session, payment.user_id, payment_months, current_payment_db_id=payment.payment_id, skip_if_active_before_payment=False
                        )

                    await session.commit()
                except Exception as exc:
                    await session.rollback()
                    logging.error("RollyPay webhook: failed to process payment %s: %s", transaction_id, exc, exc_info=True)
                    return web.Response(status=500, text="processing_error")

                db_user = payment.user or await user_dal.get_user_by_id(session, payment.user_id)
                lang = db_user.language_code if db_user and db_user.language_code else self.settings.DEFAULT_LANGUAGE
                _ = lambda k, **kw: self.i18n.gettext(lang, k, **kw) if self.i18n else k

                config_link = (
                    activation.get("subscription_url")
                    if activation
                    else None
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
                    logging.error("RollyPay webhook: failed to notify user %s: %s", payment.user_id, exc)

                try:
                    notification_service = NotificationService(self.bot, self.settings, self.i18n)
                    await notification_service.notify_payment_received(
                        user_id=payment.user_id,
                        amount=float(payment.amount),
                        currency=currency,
                        months=payment_months,
                        payment_provider="rollypay",
                        username=db_user.username if db_user else None,
                    )
                except Exception as exc:
                    logging.error("RollyPay webhook: failed to notify admins: %s", exc)

                return web.Response(text="ok")

            if status in {"CANCELED", "CANCELLED", "CHARGEBACKED"}:
                try:
                    await payment_dal.update_provider_payment_and_status(
                        session,
                        payment.payment_id,
                        transaction_id,
                        "canceled",
                    )
                    await session.commit()
                except Exception as exc:
                    await session.rollback()
                    logging.error("RollyPay webhook: failed to cancel payment %s: %s", transaction_id, exc)
                    return web.Response(status=500, text="processing_error")

                db_user = payment.user or await user_dal.get_user_by_id(session, payment.user_id)
                lang = db_user.language_code if db_user and db_user.language_code else self.settings.DEFAULT_LANGUAGE
                _ = lambda k, **kw: self.i18n.gettext(lang, k, **kw) if self.i18n else k
                try:
                    await self.bot.send_message(payment.user_id, _("payment_failed"))
                except Exception:
                    pass
                return web.Response(text="ok_canceled")

            logging.warning("RollyPay webhook: unhandled status '%s' for transaction %s", status, transaction_id)
            return web.Response(status=202, text="status_ignored")


async def rollypay_webhook_route(request: web.Request) -> web.Response:
    service: RollyPayService = request.app["rollypay_service"]
    return await service.webhook_route(request)
