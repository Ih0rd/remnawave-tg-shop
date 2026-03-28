import logging
from typing import Optional

from aiogram import F, Router, types
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards.inline.user_keyboards import get_payment_url_keyboard
from bot.middlewares.i18n import JsonI18n
from bot.services.severpay_service import SeverPayService
from config.settings import Settings
from db.dal import payment_dal

router = Router(name="user_subscription_payments_severpay_router")


async def _start_severpay_payment(
    *,
    callback: types.CallbackQuery,
    settings: Settings,
    i18n: Optional[JsonI18n],
    current_lang: str,
    severpay_service: SeverPayService,
    session: AsyncSession,
    months: int,
    price_rub: float,
    description: str,
    provider: str,
    back_callback: str,
):
    if not callback.message:
        return
    currency_code = settings.DEFAULT_CURRENCY_SYMBOL or "RUB"
    payment_record = await payment_dal.create_payment_record(
        session,
        {
            "user_id": callback.from_user.id,
            "amount": price_rub,
            "currency": currency_code,
            "status": "pending_severpay",
            "description": description,
            "subscription_duration_months": months,
            "provider": provider,
        },
    )
    await session.commit()
    success, response_data = await severpay_service.create_payment(
        payment_db_id=payment_record.payment_id,
        user_id=callback.from_user.id,
        months=months,
        amount=price_rub,
        currency=currency_code,
        description=description,
    )
    if not success:
        await payment_dal.update_payment_status_by_db_id(session, payment_record.payment_id, "failed_creation")
        await session.commit()
        return
    payment_link = response_data.get("url") or response_data.get("payment_url") or response_data.get("paymentUrl")
    provider_identifier = response_data.get("id") or response_data.get("uid")
    if provider_identifier:
        await payment_dal.update_provider_payment_and_status(
            session, payment_record.payment_id, str(provider_identifier), payment_record.status
        )
        await session.commit()
    if not payment_link:
        return
    get_text = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs) if i18n else key
    await callback.message.edit_text(
        get_text(key="payment_link_message", months=months),
        reply_markup=get_payment_url_keyboard(
            payment_link, current_lang, i18n, back_callback=back_callback, back_text_key="back_to_payment_methods_button"
        ),
        disable_web_page_preview=False,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("pay_severpay:"))
async def pay_severpay_callback_handler(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    severpay_service: SeverPayService,
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

    if not severpay_service or not severpay_service.configured:
        logging.error("SeverPay service is not configured or unavailable.")
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
        logging.error(f"Invalid pay_severpay data in callback: {callback.data}")
        try:
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception:
            pass
        return

    payment_description = get_text("payment_description_subscription", months=months)
    await _start_severpay_payment(
        callback=callback,
        settings=settings,
        i18n=i18n,
        current_lang=current_lang,
        severpay_service=severpay_service,
        session=session,
        months=months,
        price_rub=price_rub,
        description=payment_description,
        provider="severpay",
        back_callback=f"subscribe_period:{months}",
    )
    return

@router.callback_query(F.data.startswith("pay_severpay_addon:"))
async def pay_severpay_addon_callback_handler(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    severpay_service: SeverPayService,
    session: AsyncSession,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n or not callback.message:
        return
    _, package_key, months_str, price_str = callback.data.split(":")
    months = int(months_str)
    await _start_severpay_payment(
        callback=callback,
        settings=settings,
        i18n=i18n,
        current_lang=current_lang,
        severpay_service=severpay_service,
        session=session,
        months=months,
        price_rub=float(price_str),
        description=f"Addon package {package_key} ({months}m)",
        provider="severpay-addon",
        back_callback=f"addon_period:{package_key}:{months}",
    )


@router.callback_query(F.data.startswith("pay_severpay_upgrade:"))
async def pay_severpay_upgrade_callback_handler(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    severpay_service: SeverPayService,
    session: AsyncSession,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n or not callback.message:
        return
    _, months_str, price_str = callback.data.split(":")
    months = int(months_str)
    await _start_severpay_payment(
        callback=callback,
        settings=settings,
        i18n=i18n,
        current_lang=current_lang,
        severpay_service=severpay_service,
        session=session,
        months=months,
        price_rub=float(price_str),
        description=f"Squad upgrade ({months}m)",
        provider="severpay-upgrade",
        back_callback=f"squad_upgrade_period:{months}",
    )
