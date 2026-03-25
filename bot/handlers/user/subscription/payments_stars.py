import logging
from typing import Optional

from aiogram import F, Router, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from bot.middlewares.i18n import JsonI18n
from bot.services.stars_service import StarsService
from bot.services.device_package_service import DevicePackageService
from bot.services.squad_upgrade_service import SquadUpgradeService
from bot.services.notification_service import NotificationService
from db.dal import payment_dal
from db.dal import user_dal
from config.settings import Settings

router = Router(name="user_subscription_payments_stars_router")


@router.callback_query(F.data.startswith("pay_stars:"))
async def pay_stars_callback_handler(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
    stars_service: StarsService,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    get_text = (lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs) if i18n else key)

    if not i18n or not callback.message:
        try:
            await callback.answer(get_text("error_occurred_try_again"), show_alert=True)
        except Exception:
            pass
        return

    if not settings.STARS_ENABLED:
        try:
            await callback.answer(get_text("payment_service_unavailable_alert"), show_alert=True)
        except Exception:
            pass
        return

    try:
        _, data_payload = callback.data.split(":", 1)
        months_str, stars_price_str = data_payload.split(":")
        months = int(months_str)
        stars_price = int(stars_price_str)
    except (ValueError, IndexError):
        try:
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception:
            pass
        return

    user_id = callback.from_user.id
    payment_description = get_text("payment_description_subscription", months=months)

    payment_db_id = await stars_service.create_invoice(
        session=session,
        user_id=user_id,
        months=months,
        stars_price=stars_price,
        description=payment_description,
    )

    if payment_db_id:
        try:
            await callback.message.edit_text(
                get_text("payment_invoice_sent_message", months=months),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(
                        text=get_text("back_to_payment_methods_button"),
                        callback_data=f"subscribe_period:{months}",
                    )]
                ]),
            )
        except Exception as e_edit:
            logging.warning(f"Stars payment: failed to show invoice info message ({e_edit})")
        try:
            await callback.answer()
        except Exception:
            pass
        return

    try:
        await callback.answer(get_text("error_payment_gateway"), show_alert=True)
    except Exception:
        pass


@router.callback_query(F.data.startswith("pay_stars_addon:"))
async def pay_stars_addon_callback_handler(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
    device_package_service: DevicePackageService,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    get_text = (lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs) if i18n else key)
    if not callback.message:
        return
    try:
        _, package_key, months_str = callback.data.split(":")
        months = int(months_str)
    except Exception:
        await callback.answer(get_text("error_try_again"), show_alert=True)
        return
    package_cfg = settings.addon_device_packages.get(package_key)
    stars_price = None
    if package_cfg:
        stars_price = package_cfg.get("stars_prices", {}).get(months)
    if stars_price is None:
        await callback.answer(get_text("error_try_again"), show_alert=True)
        return
    try:
        payment_id = await device_package_service.create_stars_invoice(
            session,
            user_id=callback.from_user.id,
            package_key=package_key,
            months=months,
            stars_price=stars_price,
            description=get_text("extra_devices_invoice_title", months=months),
        )
        await session.commit()
    except Exception:
        await session.rollback()
        payment_id = None
    if not payment_id:
        await callback.answer(get_text("error_payment_gateway"), show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("pay_stars_upgrade:"))
async def pay_stars_upgrade_callback_handler(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
    squad_upgrade_service: SquadUpgradeService,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    get_text = (lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs) if i18n else key)
    offer = settings.squad_upgrade_offer or {}
    if not callback.message:
        return
    try:
        _, months_str = callback.data.split(":")
        months = int(months_str)
    except Exception:
        await callback.answer(get_text("error_try_again"), show_alert=True)
        return
    stars_price = offer.get("stars_prices", {}).get(months)
    if stars_price is None:
        await callback.answer(get_text("error_try_again"), show_alert=True)
        return
    try:
        payment_id = await squad_upgrade_service.create_stars_invoice(
            session,
            user_id=callback.from_user.id,
            months=months,
            stars_price=stars_price,
            description=get_text("squad_upgrade_invoice_title", months=months),
        )
        await session.commit()
    except Exception:
        await session.rollback()
        payment_id = None
    if not payment_id:
        await callback.answer(get_text("error_payment_gateway"), show_alert=True)
        return
    await callback.answer()


@router.pre_checkout_query()
async def handle_pre_checkout_query(query: types.PreCheckoutQuery):
    try:
        await query.answer(ok=True)
    except Exception:
        # Nothing else to do here; Telegram will show an error if not answered
        pass


@router.message(F.successful_payment)
async def handle_successful_stars_payment(
    message: types.Message,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
    stars_service: StarsService,
    device_package_service: DevicePackageService,
    squad_upgrade_service: SquadUpgradeService,
):
    payload = (message.successful_payment.invoice_payload
               if message and message.successful_payment else "")
    if (payload or "").startswith("addon:"):
        try:
            _, payment_db_id_str, package_key, months_str = payload.split(":")
            payment_db_id = int(payment_db_id_str)
            months = int(months_str)
            stars_amount = int(message.successful_payment.total_amount) if message.successful_payment else 0
            payment_model = await payment_dal.get_payment_by_db_id(session, payment_db_id)
            package_cfg = settings.addon_device_packages.get(package_key)
            expected_stars = None
            if package_cfg:
                expected_stars = package_cfg.get("stars_prices", {}).get(months)
            if (
                not payment_model
                or payment_model.user_id != message.from_user.id
                or payment_model.status != "pending_stars"
                or expected_stars is None
                or int(payment_model.amount) != int(expected_stars)
                or int(stars_amount) != int(expected_stars)
            ):
                await session.rollback()
                return
            await payment_dal.update_provider_payment_and_status(
                session, payment_db_id, message.successful_payment.provider_payment_charge_id, "succeeded"
            )
            expires_at = await device_package_service.activate_paid_package(
                session,
                user_id=message.from_user.id,
                package_key=package_key,
                months=months,
                provider="telegram_stars",
                payment_id=payment_db_id,
            )
            await session.commit()
            if expires_at:
                lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
                i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
                _ = lambda key, **kwargs: i18n.gettext(lang, key, **kwargs) if i18n else key
                await message.answer(
                    _("extra_devices_purchase_success", end_date=expires_at.strftime("%Y-%m-%d"))
                )
                try:
                    i18n_instance: Optional[JsonI18n] = i18n_data.get("i18n_instance")
                    notification_service = NotificationService(message.bot, settings, i18n_instance)
                    user = await user_dal.get_user_by_id(session, message.from_user.id)
                    await notification_service.notify_payment_received(
                        user_id=message.from_user.id,
                        amount=float(stars_amount),
                        currency="XTR",
                        months=months,
                        payment_provider="stars-addon",
                        username=user.username if user else None,
                    )
                except Exception:
                    pass
        except Exception:
            await session.rollback()
        return

    if (payload or "").startswith("upgrade:"):
        try:
            _, payment_db_id_str, months_str = payload.split(":")
            payment_db_id = int(payment_db_id_str)
            months = int(months_str)
            stars_amount = int(message.successful_payment.total_amount) if message.successful_payment else 0
            payment_model = await payment_dal.get_payment_by_db_id(session, payment_db_id)
            offer = settings.squad_upgrade_offer or {}
            expected_stars = offer.get("stars_prices", {}).get(months)
            if (
                not payment_model
                or payment_model.user_id != message.from_user.id
                or payment_model.status != "pending_stars"
                or expected_stars is None
                or int(payment_model.amount) != int(expected_stars)
                or int(stars_amount) != int(expected_stars)
            ):
                await session.rollback()
                return
            await payment_dal.update_provider_payment_and_status(
                session, payment_db_id, message.successful_payment.provider_payment_charge_id, "succeeded"
            )
            expires_at = await squad_upgrade_service.activate_paid_upgrade(
                session,
                user_id=message.from_user.id,
                months=months,
                provider="telegram_stars",
                payment_id=payment_db_id,
            )
            await session.commit()
            if expires_at:
                lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
                i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
                _ = lambda key, **kwargs: i18n.gettext(lang, key, **kwargs) if i18n else key
                await message.answer(_("squad_upgrade_purchase_success", end_date=expires_at.strftime("%Y-%m-%d")))
        except Exception:
            await session.rollback()
        return

    try:
        payment_db_id_str, months_str = (payload or "").split(":", 1)
        payment_db_id = int(payment_db_id_str)
        months = int(months_str)
    except Exception:
        return

    stars_amount = int(message.successful_payment.total_amount) if message.successful_payment else 0
    await stars_service.process_successful_payment(
        session=session,
        message=message,
        payment_db_id=payment_db_id,
        months=months,
        stars_amount=stars_amount,
        i18n_data=i18n_data,
    )
