import logging
from typing import Optional

from aiogram import F, Router, types
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards.inline.user_keyboards import get_payment_url_keyboard
from bot.middlewares.i18n import JsonI18n
from bot.services.freekassa_service import FreeKassaService
from config.settings import Settings
from db.dal import payment_dal

router = Router(name="user_subscription_payments_freekassa_router")


async def _start_freekassa_payment(
    *,
    callback: types.CallbackQuery,
    settings: Settings,
    i18n: Optional[JsonI18n],
    current_lang: str,
    freekassa_service: FreeKassaService,
    session: AsyncSession,
    months: int,
    price_rub: float,
    description: str,
    provider: str,
    back_callback: str,
):
    get_text = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs) if i18n else key
    if not callback.message:
        return

    user_id = callback.from_user.id
    currency_code = getattr(freekassa_service, "default_currency", None) or settings.DEFAULT_CURRENCY_SYMBOL or "RUB"
    payment_record_payload = {
        "user_id": user_id,
        "amount": price_rub,
        "currency": currency_code,
        "status": "pending_freekassa",
        "description": description,
        "subscription_duration_months": months,
        "provider": provider,
    }
    try:
        payment_record = await payment_dal.create_payment_record(session, payment_record_payload)
        await session.commit()
    except Exception as e_db_create:
        await session.rollback()
        logging.error(f"FreeKassa: failed to create payment record for user {user_id}: {e_db_create}", exc_info=True)
        await callback.answer(get_text("error_try_again"), show_alert=True)
        return
    success, response_data = await freekassa_service.create_order(
        payment_db_id=payment_record.payment_id,
        user_id=payment_record.user_id,
        months=months,
        amount=price_rub,
        currency=freekassa_service.default_currency,
        payment_method_id=freekassa_service.payment_method_id,
        ip_address=freekassa_service.server_ip,
        extra_params={"us_method": freekassa_service.payment_method_id},
    )
    if not success or not response_data.get("location"):
        await payment_dal.update_payment_status_by_db_id(session, payment_record.payment_id, "failed_creation")
        await session.commit()
        await callback.answer(get_text("error_payment_gateway"), show_alert=True)
        return
    provider_identifier = response_data.get("orderHash") or response_data.get("orderId")
    if provider_identifier:
        await payment_dal.update_provider_payment_and_status(
            session, payment_record.payment_id, str(provider_identifier), payment_record.status
        )
        await session.commit()
    await callback.message.edit_text(
        get_text(key="payment_link_message", months=months),
        reply_markup=get_payment_url_keyboard(
            response_data["location"],
            current_lang,
            i18n,
            back_callback=back_callback,
            back_text_key="back_to_payment_methods_button",
        ),
        disable_web_page_preview=False,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("pay_fk:"))
async def pay_fk_callback_handler(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    freekassa_service: FreeKassaService,
    session: AsyncSession,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    get_text = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs) if i18n else key

    if not i18n or not callback.message:
        try:
            await callback.answer(get_text("error_occurred_try_again"), show_alert=True)
        except Exception:
            pass
        return

    if not freekassa_service or not freekassa_service.configured:
        logging.error("FreeKassa service is not configured or unavailable.")
        try:
            await callback.answer(get_text("payment_service_unavailable_alert"), show_alert=True)
        except Exception:
            pass
        try:
            await callback.message.edit_text(get_text("payment_service_unavailable"))
        except Exception:
            pass
        return

    try:
        _, data_payload = callback.data.split(":", 1)
        months_str, price_str = data_payload.split(":")
        months = int(months_str)
        price_rub = float(price_str)
    except (ValueError, IndexError):
        logging.error(f"Invalid pay_fk data in callback: {callback.data}")
        try:
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception:
            pass
        return

    user_id = callback.from_user.id
    payment_description = get_text("payment_description_subscription", months=months)
    await _start_freekassa_payment(
        callback=callback,
        settings=settings,
        i18n=i18n,
        current_lang=current_lang,
        freekassa_service=freekassa_service,
        session=session,
        months=months,
        price_rub=price_rub,
        description=payment_description,
        provider="freekassa",
        back_callback=f"subscribe_period:{months}",
    )
    return

@router.callback_query(F.data.startswith("pay_fk_addon:"))
async def pay_fk_addon_callback_handler(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    freekassa_service: FreeKassaService,
    session: AsyncSession,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n or not callback.message:
        return
    try:
        _, package_key, months_str, price_str = callback.data.split(":")
        months = int(months_str)
        price = float(price_str)
    except Exception:
        await callback.answer("Invalid callback", show_alert=True)
        return
    await _start_freekassa_payment(
        callback=callback,
        settings=settings,
        i18n=i18n,
        current_lang=current_lang,
        freekassa_service=freekassa_service,
        session=session,
        months=months,
        price_rub=price,
        description=f"Addon package {package_key} ({months}m)",
        provider="freekassa-addon",
        back_callback=f"addon_period:{package_key}:{months}",
    )


@router.callback_query(F.data.startswith("pay_fk_upgrade:"))
async def pay_fk_upgrade_callback_handler(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    freekassa_service: FreeKassaService,
    session: AsyncSession,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n or not callback.message:
        return
    try:
        _, months_str, price_str = callback.data.split(":")
        months = int(months_str)
        price = float(price_str)
    except Exception:
        await callback.answer("Invalid callback", show_alert=True)
        return
    await _start_freekassa_payment(
        callback=callback,
        settings=settings,
        i18n=i18n,
        current_lang=current_lang,
        freekassa_service=freekassa_service,
        session=session,
        months=months,
        price_rub=price,
        description=f"Squad upgrade ({months}m)",
        provider="freekassa-upgrade",
        back_callback=f"squad_upgrade_period:{months}",
    )
