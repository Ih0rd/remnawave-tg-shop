import logging
from aiogram import Router, F, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from datetime import datetime, timedelta, timezone
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import Settings
from db.dal import promo_code_dal
from bot.states.admin_states import AdminStates
from bot.keyboards.inline.admin_keyboards import get_back_to_admin_panel_keyboard, get_admin_panel_keyboard
from aiogram.utils.keyboard import InlineKeyboardBuilder, InlineKeyboardButton
from bot.middlewares.i18n import JsonI18n

router = Router(name="promo_create_router")


def _format_promo_benefit_line(data: dict, _: callable) -> str:
    promo_type = data.get("promo_type", "bonus_days")
    if promo_type == "device_package":
        return _("admin_promo_benefit_line_package", default="Пакет: <b>{package_key}</b>", package_key=data.get("package_key"))
    if promo_type == "squad_upgrade":
        return _(
            "admin_promo_benefit_line_upgrade",
            default="Тип: <b>Апгрейд подписки</b> · Длительность: <b>{upgrade_days} дн.</b>",
            upgrade_days=data.get("upgrade_days", 0),
        )
    return _("admin_promo_benefit_line_bonus_days", default="Бонусные дни: <b>{bonus_days}</b>", bonus_days=data.get("bonus_days"))


async def create_promo_prompt_handler(callback: types.CallbackQuery,
                                      state: FSMContext, i18n_data: dict,
                                      settings: Settings,
                                      session: AsyncSession):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n or not callback.message:
        await callback.answer("Error preparing promo creation.",
                              show_alert=True)
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    # Step 1: Ask for promo code
    prompt_text = _(
        "admin_promo_step1_code",
        default="🎟 <b>Создание промокода</b>\n\n<b>Шаг 1 из 4:</b> Код промокода\n\nВведите код промокода (3-30 символов, только буквы и цифры):"
    )

    try:
        await callback.message.edit_text(
            prompt_text,
            reply_markup=get_back_to_admin_panel_keyboard(current_lang, i18n),
            parse_mode="HTML")
    except Exception as e:
        logging.warning(
            f"Could not edit message for promo prompt: {e}. Sending new.")
        await callback.message.answer(
            prompt_text,
            reply_markup=get_back_to_admin_panel_keyboard(current_lang, i18n),
            parse_mode="HTML")
    await callback.answer()
    await state.set_state(AdminStates.waiting_for_promo_code)


# Step 1: Process promo code
@router.message(AdminStates.waiting_for_promo_code, F.text)
async def process_promo_code_handler(message: types.Message,
                                    state: FSMContext,
                                    i18n_data: dict,
                                    settings: Settings,
                                    session: AsyncSession):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n:
        await message.reply("Language service error.")
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    try:
        code_str = message.text.strip().upper()
        if not (3 <= len(code_str) <= 30 and code_str.isalnum()):
            await message.answer(_(
                "admin_promo_invalid_code_format",
                default="❌ Код промокода должен содержать 3-30 символов (только буквы и цифры)"
            ))
            return
        
        # Check if code already exists
        existing_promo = await promo_code_dal.get_promo_code_by_code(session, code_str)
        if existing_promo:
            await message.answer(_(
                "admin_promo_code_already_exists",
                default="❌ Промокод с таким кодом уже существует"
            ))
            return
        
        await state.update_data(promo_code=code_str)
        
        # Step 2: Ask for promo type
        prompt_text = _(
            "admin_promo_step2_type",
            default="🎟 <b>Создание промокода</b>\n\n<b>Шаг 2 из 5:</b> Тип промокода\n\nКод: <b>{code}</b>\n\nВыберите тип промокода:",
            code=code_str,
        )
        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(
                text=_("admin_promo_type_bonus_days", default="🎁 Бонусные дни"),
                callback_data="promo_type:bonus_days",
            )
        )
        builder.row(
            InlineKeyboardButton(
                text=_("admin_promo_type_device_package", default="📦 Пакет устройств"),
                callback_data="promo_type:device_package",
            )
        )
        if settings.squad_upgrade_offer:
            builder.row(
                InlineKeyboardButton(
                    text=_("admin_promo_type_squad_upgrade", default="🚀 Апгрейд подписки"),
                    callback_data="promo_type:squad_upgrade",
                )
            )
        builder.row(
            InlineKeyboardButton(
                text=_("admin_back_to_panel", default="🔙 В админ панель"),
                callback_data="admin_action:main",
            )
        )
        await message.answer(
            prompt_text,
            reply_markup=builder.as_markup(),
            parse_mode="HTML",
        )
        await state.set_state(AdminStates.waiting_for_promo_type)
        
    except Exception as e:
        logging.error(f"Error processing promo code: {e}")
        await message.answer(_("error_occurred_try_again"))


# Step 2: Process promo type
@router.callback_query(
    F.data.startswith("promo_type:"),
    StateFilter(AdminStates.waiting_for_promo_type),
)
async def process_promo_type_handler(
    callback: types.CallbackQuery,
    state: FSMContext,
    i18n_data: dict,
    settings: Settings,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n or not callback.message:
        await callback.answer("Language service error.", show_alert=True)
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    promo_type = callback.data.split(":", 1)[1]
    await state.update_data(promo_type=promo_type)
    data = await state.get_data()

    if promo_type == "device_package":
        available_packages = sorted(settings.addon_device_packages.keys(), key=int)
        if not available_packages:
            await callback.answer(
                _(
                    "admin_promo_no_enabled_packages",
                    default="❌ Нет включенных пакетов устройств в настройках",
                ),
                show_alert=True,
            )
            return

        prompt_text = _(
            "admin_promo_step3_package_key",
            default="🎟 <b>Создание промокода</b>\n\n<b>Шаг 3 из 5:</b> Пакет устройств\n\nКод: <b>{code}</b>\n\nВыберите номер пакета:",
            code=data.get("promo_code"),
        )
        builder = InlineKeyboardBuilder()
        for package_key in available_packages:
            builder.row(
                InlineKeyboardButton(
                    text=_("admin_promo_package_button", default="📦 Пакет {package_key}", package_key=package_key),
                    callback_data=f"promo_package_key:{package_key}",
                )
            )
        builder.row(
            InlineKeyboardButton(
                text=_("admin_back_to_panel", default="🔙 В админ панель"),
                callback_data="admin_action:main",
            )
        )
        await callback.message.edit_text(
            prompt_text,
            reply_markup=builder.as_markup(),
            parse_mode="HTML",
        )
        await state.set_state(AdminStates.waiting_for_promo_package_key)
    elif promo_type == "squad_upgrade":
        await state.update_data(bonus_days=0, package_key=None)
        data = await state.get_data()
        prompt_text = _(
            "admin_promo_step3_upgrade_days",
            default="🎟 <b>Создание промокода</b>\n\n<b>Шаг 3 из 6:</b> Длительность апгрейда\n\nКод: <b>{code}</b>\nТип: <b>Апгрейд подписки</b>\n\nВведите длительность апгрейда в днях (1-365):",
            code=data.get("promo_code"),
        )
        await callback.message.edit_text(
            prompt_text,
            reply_markup=get_back_to_admin_panel_keyboard(current_lang, i18n),
            parse_mode="HTML",
        )
        await state.set_state(AdminStates.waiting_for_promo_upgrade_days)
    else:
        prompt_text = _(
            "admin_promo_step3_bonus_days",
            default="🎟 <b>Создание промокода</b>\n\n<b>Шаг 3 из 5:</b> Бонусные дни\n\nКод: <b>{code}</b>\n\nВведите количество бонусных дней (1-365):",
            code=data.get("promo_code"),
        )
        await callback.message.edit_text(
            prompt_text,
            reply_markup=get_back_to_admin_panel_keyboard(current_lang, i18n),
            parse_mode="HTML",
        )
        await state.set_state(AdminStates.waiting_for_promo_bonus_days)

    await callback.answer()


# Step 3 (for squad upgrade promo): Process upgrade days
@router.message(AdminStates.waiting_for_promo_upgrade_days, F.text)
async def process_promo_upgrade_days_handler(
    message: types.Message,
    state: FSMContext,
    i18n_data: dict,
    settings: Settings,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n:
        await message.reply("Language service error.")
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    try:
        upgrade_days = int(message.text.strip())
        if not (1 <= upgrade_days <= 365):
            await message.answer(
                _(
                    "admin_promo_invalid_upgrade_days",
                    default="❌ Длительность апгрейда должна быть от 1 до 365 дней",
                )
            )
            return

        await state.update_data(upgrade_days=upgrade_days)
        data = await state.get_data()
        prompt_text = _(
            "admin_promo_step4_max_activations_upgrade",
            default="🎟 <b>Создание промокода</b>\n\n<b>Шаг 4 из 6:</b> Лимит активаций\n\nКод: <b>{code}</b>\nТип: <b>Апгрейд подписки</b>\nДлительность: <b>{upgrade_days} дн.</b>\n\nВведите максимальное количество активаций (1-10000):",
            code=data.get("promo_code"),
            upgrade_days=upgrade_days,
        )
        await message.answer(
            prompt_text,
            reply_markup=get_back_to_admin_panel_keyboard(current_lang, i18n),
            parse_mode="HTML",
        )
        await state.set_state(AdminStates.waiting_for_promo_max_activations)

    except ValueError:
        await message.answer(
            _(
                "admin_promo_invalid_number",
                default="❌ Введите корректное число",
            )
        )
    except Exception as e:
        logging.error(f"Error processing promo upgrade days: {e}")
        await message.answer(_("error_occurred_try_again"))


# Step 3 (for package promo): Process package key
@router.callback_query(
    F.data.startswith("promo_package_key:"),
    StateFilter(AdminStates.waiting_for_promo_package_key),
)
async def process_promo_package_key_handler(
    callback: types.CallbackQuery,
    state: FSMContext,
    i18n_data: dict,
    settings: Settings,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n or not callback.message:
        await callback.answer("Language service error.", show_alert=True)
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    package_key = callback.data.split(":", 1)[1]
    if package_key not in settings.addon_device_packages:
        await callback.answer(
            _(
                "admin_promo_invalid_package_key",
                default="❌ Номер пакета недоступен",
            ),
            show_alert=True,
        )
        return

    await state.update_data(package_key=package_key, bonus_days=0)
    data = await state.get_data()
    prompt_text = _(
        "admin_promo_step4_max_activations_package",
        default="🎟 <b>Создание промокода</b>\n\n<b>Шаг 4 из 5:</b> Лимит активаций\n\nКод: <b>{code}</b>\nПакет: <b>{package_key}</b>\n\nВведите максимальное количество активаций (1-10000):",
        code=data.get("promo_code"),
        package_key=package_key,
    )

    await callback.message.edit_text(
        prompt_text,
        reply_markup=get_back_to_admin_panel_keyboard(current_lang, i18n),
        parse_mode="HTML",
    )
    await state.set_state(AdminStates.waiting_for_promo_max_activations)
    await callback.answer()


# Step 3 (for bonus promo): Process bonus days
@router.message(AdminStates.waiting_for_promo_bonus_days, F.text)
async def process_promo_bonus_days_handler(message: types.Message,
                                          state: FSMContext,
                                          i18n_data: dict,
                                          settings: Settings):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n:
        await message.reply("Language service error.")
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    try:
        bonus_days = int(message.text.strip())
        if not (1 <= bonus_days <= 365):
            await message.answer(_(
                "admin_promo_invalid_bonus_days",
                default="❌ Количество бонусных дней должно быть от 1 до 365"
            ))
            return
        
        await state.update_data(bonus_days=bonus_days)
        
        # Step 4: Ask for max activations
        data = await state.get_data()
        prompt_text = _(
            "admin_promo_step4_max_activations",
            default="🎟 <b>Создание промокода</b>\n\n<b>Шаг 4 из 5:</b> Лимит активаций\n\nКод: <b>{code}</b>\nБонусные дни: <b>{bonus_days}</b>\n\nВведите максимальное количество активаций (1-10000):",
            code=data.get("promo_code"),
            bonus_days=bonus_days
        )
        
        await message.answer(
            prompt_text,
            reply_markup=get_back_to_admin_panel_keyboard(current_lang, i18n),
            parse_mode="HTML"
        )
        await state.set_state(AdminStates.waiting_for_promo_max_activations)
        
    except ValueError:
        await message.answer(_(
            "admin_promo_invalid_number",
            default="❌ Введите корректное число"
        ))
    except Exception as e:
        logging.error(f"Error processing promo bonus days: {e}")
        await message.answer(_("error_occurred_try_again"))


# Step 3: Process max activations
@router.message(AdminStates.waiting_for_promo_max_activations, F.text)
async def process_promo_max_activations_handler(message: types.Message,
                                               state: FSMContext,
                                               i18n_data: dict,
                                               settings: Settings):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n:
        await message.reply("Language service error.")
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    try:
        max_activations = int(message.text.strip())
        if not (1 <= max_activations <= 10000):
            await message.answer(_(
                "admin_promo_invalid_max_activations",
                default="❌ Максимальное количество активаций должно быть от 1 до 10000"
            ))
            return
        
        await state.update_data(max_activations=max_activations)
        
        # Step 5: Ask for validity
        data = await state.get_data()
        prompt_text = _(
            "admin_promo_step5_validity",
            default="🎟 <b>Создание промокода</b>\n\n<b>Шаг 5 из 5:</b> Срок действия\n\nКод: <b>{code}</b>\n{benefit_line}\nМакс. активаций: <b>{max_activations}</b>\n\nВыберите срок действия промокода:",
            code=data.get("promo_code"),
            benefit_line=_format_promo_benefit_line(data, _),
            max_activations=max_activations
        )
        
        # Create keyboard for validity options
        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(
                text=_("admin_promo_unlimited_validity", default="🔄 Без ограничений"),
                callback_data="promo_unlimited_validity"
            )
        )
        builder.row(
            InlineKeyboardButton(
                text=_("admin_promo_set_validity_days", default="📅 Указать дни"),
                callback_data="promo_set_validity"
            )
        )
        builder.row(
            InlineKeyboardButton(
                text=_("admin_back_to_panel", default="🔙 В админ панель"),
                callback_data="admin_action:main"
            )
        )
        
        await message.answer(
            prompt_text,
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await state.set_state(AdminStates.waiting_for_promo_validity_days)
        
    except ValueError:
        await message.answer(_(
            "admin_promo_invalid_number",
            default="❌ Введите корректное число"
        ))
    except Exception as e:
        logging.error(f"Error processing promo max activations: {e}")
        await message.answer(_("error_occurred_try_again"))


# Step 4: Handle unlimited validity
@router.callback_query(F.data == "promo_unlimited_validity", StateFilter(AdminStates.waiting_for_promo_validity_days))
async def process_promo_unlimited_validity(callback: types.CallbackQuery,
                                          state: FSMContext,
                                          i18n_data: dict,
                                          settings: Settings,
                                          session: AsyncSession):
    await state.update_data(validity_days=None)
    await create_promo_code_final(callback, state, i18n_data, settings, session)


# Step 4: Handle set validity
@router.callback_query(F.data == "promo_set_validity", StateFilter(AdminStates.waiting_for_promo_validity_days))
async def process_promo_set_validity(callback: types.CallbackQuery,
                                    state: FSMContext,
                                    i18n_data: dict,
                                    settings: Settings):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n or not callback.message:
        await callback.answer("Error processing validity.", show_alert=True)
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    data = await state.get_data()
    benefit_line = _format_promo_benefit_line(data, _)
    prompt_text = _(
        "admin_promo_enter_validity_days",
        default="🎟 <b>Создание промокода</b>\n\n<b>Шаг 5 из 5:</b> Срок действия\n\nКод: <b>{code}</b>\n{benefit_line}\nМакс. активаций: <b>{max_activations}</b>\n\nВведите количество дней действия промокода (1-365):",
        code=data.get("promo_code"),
        benefit_line=benefit_line,
        max_activations=data.get("max_activations")
    )
    
    try:
        await callback.message.edit_text(
            prompt_text,
            reply_markup=get_back_to_admin_panel_keyboard(current_lang, i18n),
            parse_mode="HTML"
        )
    except Exception:
        await callback.message.answer(
            prompt_text,
            reply_markup=get_back_to_admin_panel_keyboard(current_lang, i18n),
            parse_mode="HTML"
        )
    await callback.answer()


# Step 4: Process validity days
@router.message(AdminStates.waiting_for_promo_validity_days, F.text)
async def process_promo_validity_days_handler(message: types.Message,
                                             state: FSMContext,
                                             i18n_data: dict,
                                             settings: Settings,
                                             session: AsyncSession):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n:
        await message.reply("Language service error.")
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    try:
        validity_days = int(message.text.strip())
        if not (1 <= validity_days <= 365):
            await message.answer(_(
                "admin_promo_invalid_validity_days",
                default="❌ Срок действия должен быть от 1 до 365 дней"
            ))
            return
        
        await state.update_data(validity_days=validity_days)
        await create_promo_code_final(message, state, i18n_data, settings, session)
        
    except ValueError:
        await message.answer(_(
            "admin_promo_invalid_number",
            default="❌ Введите корректное число"
        ))
    except Exception as e:
        logging.error(f"Error processing promo validity days: {e}")
        await message.answer(_("error_occurred_try_again"))


async def create_promo_code_final(callback_or_message,
                                 state: FSMContext,
                                 i18n_data: dict,
                                 settings: Settings,
                                 session: AsyncSession):
    """Final step - create the promo code in database"""
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n:
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    try:
        data = await state.get_data()
        
        # Prepare promo code data
        promo_data = {
            "code": data["promo_code"],
            "promo_type": data.get("promo_type", "bonus_days"),
            "bonus_days": data.get("bonus_days", 0),
            "upgrade_days": data.get("upgrade_days", 0),
            "package_key": data.get("package_key"),
            "max_activations": data["max_activations"],
            "current_activations": 0,
            "is_active": True,
            "created_by_admin_id": callback_or_message.from_user.id,
            "created_at": datetime.now(timezone.utc)
        }
        
        # Set validity
        if data.get("validity_days"):
            promo_data["valid_until"] = datetime.now(timezone.utc) + timedelta(days=data["validity_days"])
        else:
            promo_data["valid_until"] = None
        
        # Create promo code
        created_promo = await promo_code_dal.create_promo_code(session, promo_data)
        await session.commit()
        
        # Log successful creation
        logging.info(f"Promo code '{data['promo_code']}' created with ID {created_promo.promo_code_id}")
        
        # Success message
        valid_until_str = _("admin_promo_unlimited", default="Без ограничений") if not data.get("validity_days") else f"{data['validity_days']} дней"
        benefit_line = (
            _("admin_promo_created_success_benefit_package", package_key=data.get("package_key"))
            if data.get("promo_type") == "device_package"
            else (
                _("admin_promo_created_success_benefit_upgrade", upgrade_days=data.get("upgrade_days", 0))
                if data.get("promo_type") == "squad_upgrade"
                else _("admin_promo_created_success_benefit_days", bonus_days=data.get("bonus_days", 0))
            )
        )
        success_text = _(
            "admin_promo_created_success",
            default="✅ <b>Промокод успешно создан!</b>\n\n"
                   "🎟 Код: <code>{code}</code>\n"
                   "{benefit_line}\n"
                   "📊 Макс. активаций: <b>{max_activations}</b>\n"
                   "⏰ Срок действия: <b>{valid_until_str}</b>",
            code=data["promo_code"],
            benefit_line=benefit_line,
            max_activations=data["max_activations"],
            valid_until_str=valid_until_str
        )
        
        if hasattr(callback_or_message, 'message'):  # CallbackQuery
            try:
                await callback_or_message.message.edit_text(
                    success_text,
                    reply_markup=get_back_to_admin_panel_keyboard(current_lang, i18n),
                    parse_mode="HTML"
                )
            except Exception:
                await callback_or_message.message.answer(
                    success_text,
                    reply_markup=get_back_to_admin_panel_keyboard(current_lang, i18n),
                    parse_mode="HTML"
                )
            await callback_or_message.answer()
        else:  # Message
            await callback_or_message.answer(
                success_text,
                reply_markup=get_back_to_admin_panel_keyboard(current_lang, i18n),
                parse_mode="HTML"
            )
        
        await state.clear()
        
    except Exception as e:
        logging.error(f"Error creating promo code: {e}")
        error_text = _("error_occurred_try_again", default="❌ Произошла ошибка. Попробуйте снова.")
        
        if hasattr(callback_or_message, 'message'):  # CallbackQuery
            await callback_or_message.message.answer(error_text)
            await callback_or_message.answer()
        else:  # Message
            await callback_or_message.answer(error_text)
        
        await state.clear()


# Cancel promo creation
@router.callback_query(
    F.data == "admin_action:main",
    StateFilter(
        AdminStates.waiting_for_promo_code,
        AdminStates.waiting_for_promo_type,
        AdminStates.waiting_for_promo_bonus_days,
        AdminStates.waiting_for_promo_package_key,
        AdminStates.waiting_for_promo_upgrade_days,
        AdminStates.waiting_for_promo_max_activations,
        AdminStates.waiting_for_promo_validity_days,
    ),
)
async def cancel_promo_creation_state_to_menu(callback: types.CallbackQuery,
                                              state: FSMContext,
                                              settings: Settings,
                                              i18n_data: dict,
                                              session: AsyncSession):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n or not callback.message:
        await callback.answer("Error cancelling.", show_alert=True)
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    try:
        await callback.message.edit_text(
            _(key="admin_panel_title"),
            reply_markup=get_admin_panel_keyboard(i18n, current_lang, settings)
        )
    except Exception:
        await callback.message.answer(
            _(key="admin_panel_title"),
            reply_markup=get_admin_panel_keyboard(i18n, current_lang, settings)
        )
    
    await callback.answer(_("admin_promo_creation_cancelled", default="Создание промокода отменено"))
    await state.clear()
