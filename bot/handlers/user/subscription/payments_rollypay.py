import json
import logging
from typing import Optional

from aiogram import F, Router, types
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards.inline.user_keyboards import get_payment_url_keyboard
from bot.middlewares.i18n import JsonI18n
from bot.services.rollypay_service import RollyPayService
from config.settings import Settings
from db.dal import payment_dal

router = Router(name="user_subscription_payments_rollypay_router")


async def _start_rollypay_payment(
    *,
    callback: types.CallbackQuery,
    settings: Settings,
    i18n: Optional[JsonI18n],
    current_lang: str,
    rollypay_service: RollyPayService,
    session: AsyncSession,
    months: int,
    price_rub: float,
    description: str,
    provider: str,
    back_callback: str,
):
    if not callback.message:
        return
    user_id = callback.from_user.id
    currency_code = settings.DEFAULT_CURRENCY_SYMBOL or "RUB"
    payment_record = await payment_dal.create_payment_record(
        session,
        {
            "user_id": user_id,
            "amount": price_rub,
            "currency": currency_code,
            "status": "pending_rollypay",
            "description": description,
            "subscription_duration_months": months,
            "provider": provider,
        },
    )
    await session.commit()
    payload_meta = json.dumps({"payment_db_id": payment_record.payment_id, "user_id": user_id, "months": months})
    success, response_data = await rollypay_service.create_transaction(
        payment_db_id=payment_record.payment_id,
        user_id=user_id,
        months=months,
        amount=price_rub,
        currency=currency_code,
        description=description,
        payload=payload_meta,
    )
    if not success:
        await payment_dal.update_payment_status_by_db_id(session, payment_record.payment_id, "failed_creation")
        await session.commit()
        return
    transaction_id = response_data.get("transactionId") or response_data.get("id")
    redirect_url = response_data.get("redirect") or response_data.get("url") or response_data.get("paymentUrl")
    if not transaction_id or not redirect_url:
        await payment_dal.update_payment_status_by_db_id(session, payment_record.payment_id, "failed_creation")
        await session.commit()
        return
    await payment_dal.update_provider_payment_and_status(
        session, payment_record.payment_id, str(transaction_id), str(response_data.get("status", payment_record.status))
    )
    await session.commit()
    get_text = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs) if i18n else key
    await callback.message.edit_text(
        get_text(key="payment_link_message", months=months),
        reply_markup=get_payment_url_keyboard(
            redirect_url, current_lang, i18n, back_callback=back_callback, back_text_key="back_to_payment_methods_button"
        ),
        disable_web_page_preview=False,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("pay_rollypay:"))
async def pay_rollypay_callback_handler(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    rollypay_service: RollyPayService,
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

    if not rollypay_service or not rollypay_service.configured:
        logging.error("RollyPay service is not configured or unavailable.")
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
        logging.error(f"Invalid pay_rollypay data in callback: {callback.data}")
        try:
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception:
            pass
        return

    payment_description = get_text("payment_description_subscription", months=months)
    await _start_rollypay_payment(
        callback=callback,
        settings=settings,
        i18n=i18n,
        current_lang=current_lang,
        rollypay_service=rollypay_service,
        session=session,
        months=months,
        price_rub=price_rub,
        description=payment_description,
        provider="rollypay",
        back_callback=f"subscribe_period:{months}",
    )
    return

@router.callback_query(F.data.startswith("pay_rollypay_addon:"))
async def pay_rollypay_addon_callback_handler(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    rollypay_service: RollyPayService,
    session: AsyncSession,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n or not callback.message:
        return
    _, package_key, months_str, price_str = callback.data.split(":")
    months = int(months_str)
    await _start_rollypay_payment(
        callback=callback,
        settings=settings,
        i18n=i18n,
        current_lang=current_lang,
        rollypay_service=rollypay_service,
        session=session,
        months=months,
        price_rub=float(price_str),
        description=f"Addon package {package_key} ({months}m)",
        provider="rollypay-addon",
        back_callback=f"addon_period:{package_key}:{months}",
    )


@router.callback_query(F.data.startswith("pay_rollypay_upgrade:"))
async def pay_rollypay_upgrade_callback_handler(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    rollypay_service: RollyPayService,
    session: AsyncSession,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n or not callback.message:
        return
    _, months_str, price_str = callback.data.split(":")
    months = int(months_str)
    await _start_rollypay_payment(
        callback=callback,
        settings=settings,
        i18n=i18n,
        current_lang=current_lang,
        rollypay_service=rollypay_service,
        session=session,
        months=months,
        price_rub=float(price_str),
        description=f"Squad upgrade ({months}m)",
        provider="rollypay-upgrade",
        back_callback=f"squad_upgrade_period:{months}",
    )
