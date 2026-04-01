import logging
import re
from aiogram import Router, F, types, Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.utils.markdown import hcode, hbold
from typing import Optional, Dict, Any, Callable, Awaitable
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timezone

from config.settings import Settings
from db.dal import user_dal, subscription_dal, message_log_dal
from db.models import User
from bot.states.admin_states import AdminStates
from bot.keyboards.inline.admin_keyboards import get_back_to_admin_panel_keyboard
from bot.services.subscription_service import SubscriptionService
from bot.services.panel_api_service import PanelApiService
from bot.services.referral_service import ReferralService
from bot.middlewares.i18n import JsonI18n
from bot.utils import get_message_content, send_direct_message
from aiogram.utils.keyboard import InlineKeyboardBuilder, InlineKeyboardButton
from bot.utils.text_sanitizer import (
    sanitize_display_name,
    sanitize_username,
    username_for_display,
)
from bot.utils.telegram_markup import (
    is_profile_link_error,
    remove_profile_link_buttons,
)

router = Router(name="admin_user_management_router")
USERNAME_REGEX = re.compile(r"^[a-zA-Z0-9_]{5,32}$")


async def users_list_handler(callback: types.CallbackQuery,
                              i18n_data: dict, settings: Settings,
                              session: AsyncSession, page: int = 0):
    """Display paginated list of all users"""
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n or not callback.message:
        await callback.answer("Error preparing user list.", show_alert=True)
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)
    
    try:
        # Get paginated users
        from bot.keyboards.inline.admin_keyboards import get_users_list_keyboard
        from db.dal import user_dal
        
        users = await user_dal.get_all_users_paginated(session, page=page, page_size=15)
        total_users = await user_dal.count_all_users(session)
        total_pages = max(1, (total_users + 14) // 15)
        
        # Format message
        header_text = _(
            "admin_users_list_header",
            default="👥 <b>Список пользователей</b>\n\nСтраница {current}/{total} ({total_users} пользователей)",
            current=page + 1,
            total=total_pages,
            total_users=total_users
        )
        
        keyboard = get_users_list_keyboard(users, page, total_users, i18n, current_lang, page_size=15)
        
        await callback.message.edit_text(
            header_text,
            reply_markup=keyboard,
            parse_mode="HTML"
        )
        await callback.answer()
        
    except Exception as e:
        logging.error(f"Error displaying user list: {e}")
        await callback.answer("Ошибка отображения списка пользователей", show_alert=True)


async def user_search_prompt_handler(callback: types.CallbackQuery,
                                     state: FSMContext, i18n_data: dict,
                                     settings: Settings, session: AsyncSession):
    """Display search prompt for user management"""
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n or not callback.message:
        await callback.answer("Error preparing search.", show_alert=True)
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    prompt_text = _(
        "admin_user_management_prompt",
        default="👤 Управление пользователями\n\nВведите ID пользователя или @username для поиска:"
    )

    try:
        await callback.message.edit_text(
            prompt_text,
            reply_markup=get_back_to_admin_panel_keyboard(current_lang, i18n)
        )
    except Exception as e:
        logging.warning(f"Could not edit message for user management: {e}. Sending new.")
        await callback.message.answer(
            prompt_text,
            reply_markup=get_back_to_admin_panel_keyboard(current_lang, i18n)
        )
    
    await callback.answer()
    await state.set_state(AdminStates.waiting_for_user_search)


def get_user_card_keyboard(user_id: int, i18n_instance, lang: str,
                           referrer_id: Optional[int] = None) -> InlineKeyboardBuilder:
    """Generate keyboard for user management actions"""
    _ = lambda key, **kwargs: i18n_instance.gettext(lang, key, **kwargs)
    builder = InlineKeyboardBuilder()
    
    # Row 1: Trial and Subscription actions
    builder.button(
        text=_(key="admin_user_reset_trial_button", default="🔄 Сбросить триал"),
        callback_data=f"user_action:reset_trial:{user_id}"
    )
    builder.button(
        text=_(key="admin_user_add_subscription_button", default="➕ Добавить дни"),
        callback_data=f"user_action:add_subscription:{user_id}"
    )
    
    # Row 2: Block/Unblock and Message
    builder.button(
        text=_(key="admin_user_toggle_ban_button", default="🚫 Заблокировать/Разблокировать"),
        callback_data=f"user_action:toggle_ban:{user_id}"
    )
    builder.button(
        text=_(key="admin_user_send_message_button", default="✉️ Отправить сообщение"),
        callback_data=f"user_action:send_message:{user_id}"
    )
    
    # Row 3: View actions
    builder.button(
        text=_(key="admin_user_view_logs_button", default="📜 Действия пользователя"),
        callback_data=f"user_action:view_logs:{user_id}"
    )
    builder.button(
        text=_(key="admin_user_ip_control_button", default="🌐 IP-control"),
        callback_data=f"user_action:ip_control:{user_id}"
    )
    builder.button(
        text=_(key="admin_user_refresh_button", default="🔄 Обновить"),
        callback_data=f"user_action:refresh:{user_id}"
    )

    # Row 4: Quick links
    builder.button(
        text=_(key="user_card_open_profile_button",
               default="👤 Открыть профиль"),
        url=f"tg://user?id={user_id}"
    )
    if referrer_id:
        builder.button(
            text=_(key="user_card_open_referrer_profile_button",
                   default="👤 Открыть профиль пригласившего"),
            url=f"tg://user?id={referrer_id}"
        )

    # Row 5: Destructive action
    builder.button(
        text=_(key="admin_user_delete_button", default="❌ Удалить пользователя"),
        callback_data=f"user_action:delete_user:{user_id}"
    )
    
    # Row 6: Navigation
    builder.button(
        text=_(key="admin_user_search_new_button", default="🔍 Найти другого"),
        callback_data="admin_action:users_management"
    )
    builder.button(
        text=_(key="back_to_admin_panel_button"),
        callback_data="admin_action:main"
    )
    
    quick_links_width = 2 if referrer_id else 1
    builder.adjust(2, 2, 3, quick_links_width, 1, 2)
    return builder


async def _send_with_profile_link_fallback(
        sender: Callable[..., Awaitable[Any]],
        *,
        text: str,
        markup: Optional[types.InlineKeyboardMarkup],
        user_id: int,
        parse_mode: Optional[str] = "HTML") -> None:
    """Send text with markup and fallback if Telegram rejects tg://user buttons."""
    send_kwargs: Dict[str, Any] = {"text": text, "reply_markup": markup}
    if parse_mode is not None:
        send_kwargs["parse_mode"] = parse_mode

    try:
        await sender(**send_kwargs)
    except TelegramBadRequest as exc:
        if not is_profile_link_error(exc):
            raise

        logging.warning(
            "Telegram rejected profile buttons for user %s: %s. Retrying without tg:// links.",
            user_id,
            getattr(exc, "message", "") or str(exc),
        )
        fallback_markup = remove_profile_link_buttons(markup)
        send_kwargs["reply_markup"] = fallback_markup
        await sender(**send_kwargs)


async def format_user_card(user: User, session: AsyncSession, 
                          subscription_service: SubscriptionService,
                          i18n_instance, lang: str,
                          referral_service: Optional[ReferralService] = None) -> str:
    """Format user information as a detailed card"""
    _ = lambda key, **kwargs: i18n_instance.gettext(lang, key, **kwargs)
    
    # Basic user info
    card_parts = []
    card_parts.append(f"👤 <b>{_('admin_user_card_title', default='Карточка пользователя')}</b>\n")
    
    # User details
    na_value = _("admin_user_na_value", default="N/A")
    safe_first_name = sanitize_display_name(user.first_name) if user.first_name else None
    user_name = safe_first_name or na_value
    if user.username:
        sanitized_username = sanitize_username(user.username)
        if sanitized_username:
            username_display = f"@{sanitized_username}"
        else:
            username_display = username_for_display(user.username, with_at=False)
    else:
        username_display = na_value
    registration_date = user.registration_date.strftime('%Y-%m-%d %H:%M') if user.registration_date else na_value
    
    card_parts.append(f"{_('admin_user_id_label', default='🆔 <b>ID:</b>')} {hcode(str(user.user_id))}")
    card_parts.append(f"{_('admin_user_name_label', default='👤 <b>Имя:</b>')} {hcode(user_name)}")
    card_parts.append(f"{_('admin_user_username_label', default='📱 <b>Username:</b>')} {hcode(username_display)}")
    card_parts.append(f"{_('admin_user_language_label', default='🌍 <b>Язык:</b>')} {hcode(user.language_code or na_value)}")
    card_parts.append(f"{_('admin_user_registration_label', default='📅 <b>Регистрация:</b>')} {hcode(registration_date)}")
    
    # Ban status
    ban_status = _("admin_user_status_banned", default="🚫 Заблокирован") if user.is_banned else _("admin_user_status_active", default="✅ Активен")
    card_parts.append(f"{_('admin_user_status_label', default='🛡 <b>Статус:</b>')} {ban_status}")
    
    # Referral info
    if user.referred_by_id:
        card_parts.append(f"{_('admin_user_referral_label', default='🎁 <b>Привлечен по реферальной программе от:</b>')} {hcode(str(user.referred_by_id))}")
    
    # Panel info
    if user.panel_user_uuid:
        card_parts.append(f"{_('admin_user_panel_uuid_label', default='🔗 <b>Panel UUID:</b>')} {hcode(user.panel_user_uuid[:8] + '...' if len(user.panel_user_uuid) > 8 else user.panel_user_uuid)}")

        # IP diagnostics from panel nodes (Remnawave v2.7.0+)
        try:
            panel_service = getattr(subscription_service, "panel_service", None)
            if panel_service:
                diagnostics = await panel_service.get_user_ip_diagnostics(
                    user_uuid=user.panel_user_uuid,
                    max_nodes=2,
                )
                if diagnostics:
                    card_parts.append(
                        f"{_('admin_user_ip_diag_title', default='🌐 <b>IP диагностика по нодам:</b>')}"
                    )
                    for node_item in diagnostics:
                        node_name = node_item.get("node_name", "Node")
                        ips = node_item.get("ips", [])
                        if not ips:
                            continue
                        first_ip = ips[0]
                        ip_value = first_ip.get("ip", "N/A")
                        last_seen = first_ip.get("lastSeen", "")
                        if last_seen:
                            card_parts.append(
                                _("admin_user_ip_diag_item_with_seen",
                                  default="• {node}: {ip} (last seen: {last_seen})",
                                  node=node_name,
                                  ip=ip_value,
                                  last_seen=last_seen)
                            )
                        else:
                            card_parts.append(
                                _("admin_user_ip_diag_item",
                                  default="• {node}: {ip}",
                                  node=node_name,
                                  ip=ip_value)
                            )
                else:
                    card_parts.append(
                        _("admin_user_ip_diag_empty",
                          default="🌐 <b>IP диагностика:</b> данных пока нет")
                    )
        except Exception as e_diag:
            logging.warning("Failed to fetch IP diagnostics for user %s: %s", user.user_id, e_diag)
    
    card_parts.append("")  # Empty line
    
    # Subscription info
    try:
        subscription_details = await subscription_service.get_active_subscription_details(session, user.user_id)
        if subscription_details:
            card_parts.append(f"💳 <b>{_('admin_user_subscription_info', default='Информация о подписке:')}</b>")
            
            end_date = subscription_details.get('end_date')
            if end_date:
                end_date_str = end_date.strftime('%Y-%m-%d %H:%M') if isinstance(end_date, datetime) else str(end_date)
                card_parts.append(f"{_('admin_user_subscription_active_until', default='⏰ <b>Действует до:</b>')} {hcode(end_date_str)}")
            
            status = subscription_details.get('status_from_panel', 'UNKNOWN')
            card_parts.append(f"{_('admin_user_panel_status_label', default='📊 <b>Статус на панели:</b>')} {hcode(status)}")
            
            traffic_limit = subscription_details.get('traffic_limit_bytes')
            traffic_used = subscription_details.get('traffic_used_bytes')
            if traffic_limit and traffic_used is not None:
                traffic_limit_gb = traffic_limit / (1024**3)
                traffic_used_gb = traffic_used / (1024**3)
                card_parts.append(f"{_('admin_user_traffic_label', default='📊 <b>Трафик:</b>')} {hcode(f'{traffic_used_gb:.2f}GB / {traffic_limit_gb:.2f}GB')}")
        else:
            card_parts.append(f"{_('admin_user_subscription_label', default='💼 <b>Подписка:</b>')} {hcode(_('admin_user_subscription_none', default='Нет активной подписки'))}")
    except Exception as e:
        logging.error(f"Error getting subscription details for user {user.user_id}: {e}")
        card_parts.append(f"{_('admin_user_subscription_label', default='💼 <b>Подписка:</b>')} {hcode(_('admin_user_subscription_error', default='Ошибка загрузки'))}")
    
    # Statistics
    try:
        # Count user logs
        logs_count = await message_log_dal.count_user_message_logs(session, user.user_id)
        card_parts.append(f"{_('admin_user_actions_count_label', default='📜 <b>Всего действий:</b>')} {hcode(str(logs_count))}")
        
        # Check if user had any subscriptions
        had_subscriptions = await subscription_service.has_had_any_subscription(session, user.user_id)
        trial_status = _("admin_user_trial_used", default="Использовал") if had_subscriptions else _("admin_user_trial_not_used", default="Не использовал")
        card_parts.append(f"{_('admin_user_trial_label', default='🏡 <b>Триал:</b>')} {hcode(trial_status)}")

        # Financial analytics (admin-only)
        try:
            from db.dal import payment_dal
            
            # Total amount paid by this user
            total_paid = await payment_dal.get_user_total_paid(session, user.user_id)
            card_parts.append(f"{_('admin_user_total_paid_label', default='💰 <b>Всего оплачено:</b>')} {hcode(f'{total_paid:.2f} RUB')}")
            
            # Total revenue from referrals
            referral_revenue = await payment_dal.get_referral_revenue(session, user.user_id)
            card_parts.append(f"{_('admin_user_referral_revenue_label', default='💸 <b>Доход по рефералам:</b>')} {hcode(f'{referral_revenue:.2f} RUB')}")
        except Exception as e_fin:
            logging.error(f"Failed to build financial analytics for admin card {user.user_id}: {e_fin}")

        # Referral stats
        if referral_service is not None:
            try:
                stats = await referral_service.get_referral_stats(session, user.user_id)
                invited_count = stats.get('invited_count', 0)
                purchased_count = stats.get('purchased_count', 0)
                card_parts.append(f"{_('admin_user_invited_friends_label', default='👥 <b>Приглашено друзей:</b>')} {hcode(str(invited_count))}")
                card_parts.append(f"{_('admin_user_ref_purchased_label', default='💳 <b>Купили подписку:</b>')} {hcode(str(purchased_count))}")
            except Exception as e_rs:
                logging.error(f"Failed to build referral stats for admin card {user.user_id}: {e_rs}")
        
    except Exception as e:
        logging.error(f"Error getting user statistics for {user.user_id}: {e}")
    
    return "\n".join(card_parts)


@router.message(AdminStates.waiting_for_user_search, F.text)
async def process_user_search_handler(message: types.Message, state: FSMContext,
                                     settings: Settings, i18n_data: dict,
                                     subscription_service: SubscriptionService,
                                     session: AsyncSession):
    """Process user search input and display user card"""
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n:
        await message.reply("Language service error.")
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    input_text = message.text.strip() if message.text else ""
    user_model: Optional[User] = None

    # Try to find user by ID or username
    if input_text.isdigit():
        try:
            user_model = await user_dal.get_user_by_id(session, int(input_text))
        except ValueError:
            pass
    elif input_text.startswith("@") and USERNAME_REGEX.match(input_text[1:]):
        user_model = await user_dal.get_user_by_username(session, input_text[1:])
    elif USERNAME_REGEX.match(input_text):
        user_model = await user_dal.get_user_by_username(session, input_text)

    if not user_model:
        await message.answer(_(
            "admin_user_not_found",
            default="❌ Пользователь не найден: {input}",
            input=hcode(input_text)
        ))
        return

    # Store user ID in state for further operations
    await state.update_data(target_user_id=user_model.user_id)
    await state.clear()

    # Format and send user card
    try:
        referral_service = ReferralService(settings, subscription_service, message.bot, i18n)
        user_card_text = await format_user_card(user_model, session, subscription_service, i18n, current_lang, referral_service)
        keyboard = get_user_card_keyboard(
            user_model.user_id,
            i18n,
            current_lang,
            user_model.referred_by_id
        )
        
        await _send_with_profile_link_fallback(
            message.answer,
            text=user_card_text,
            markup=keyboard.as_markup(),
            user_id=user_model.user_id,
            parse_mode="HTML"
        )
    except Exception as e:
        logging.error(f"Error displaying user card for {user_model.user_id}: {e}")
        await message.answer(_(
            "admin_user_card_error",
            default="❌ Ошибка отображения карточки пользователя"
        ))


@router.callback_query(F.data.startswith("user_action:"))
async def user_action_handler(callback: types.CallbackQuery, state: FSMContext,
                             settings: Settings, i18n_data: dict, bot: Bot,
                             subscription_service: SubscriptionService,
                             panel_service: PanelApiService,
                             session: AsyncSession):
    """Handle user management actions"""
    try:
        parts = callback.data.split(":")
        action = parts[1]
        user_id = int(parts[2])
    except (IndexError, ValueError):
        await callback.answer("Invalid action format.", show_alert=True)
        return

    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n:
        await callback.answer("Language service error.", show_alert=True)
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    # Get user from database
    user = await user_dal.get_user_by_id(session, user_id)
    if not user:
        await callback.answer(_(
            "admin_user_not_found_action",
            default="Пользователь не найден"
        ), show_alert=True)
        return

    if action == "reset_trial":
        await handle_reset_trial(callback, user, subscription_service, session, i18n, current_lang)
    elif action == "add_subscription":
        await handle_add_subscription_prompt(callback, state, user, i18n, current_lang)
    elif action == "toggle_ban":
        await handle_toggle_ban(callback, user, panel_service, session, i18n, current_lang)
    elif action == "send_message":
        await handle_send_message_prompt(callback, state, user, i18n, current_lang)
    elif action == "view_logs":
        await handle_view_user_logs(callback, user, session, settings, i18n, current_lang)
    elif action == "ip_control":
        await handle_ip_control_view(callback, user, panel_service, i18n, current_lang)
    elif action == "refresh":
        await handle_refresh_user_card(callback, user, subscription_service, session, i18n, current_lang)
    elif action == "delete_user":
        await handle_delete_user_prompt(
            callback, state, user, settings, i18n, current_lang, session
        )
    else:
        await callback.answer(_("admin_unknown_action"), show_alert=True)


async def handle_reset_trial(callback: types.CallbackQuery, user: User,
                           subscription_service: SubscriptionService,
                           session: AsyncSession, i18n_instance, lang: str):
    """Reset user's trial eligibility"""
    _ = lambda key, **kwargs: i18n_instance.gettext(lang, key, **kwargs)
    
    try:
        # Delete all user subscriptions to reset trial eligibility
        await subscription_dal.delete_all_user_subscriptions(session, user.user_id)
        await session.commit()
        
        await callback.answer(_(
            "admin_user_trial_reset_success",
            default="✅ Триал сброшен! Пользователь может активировать триал заново."
        ), show_alert=True)
        
        # Refresh user card
        await handle_refresh_user_card(callback, user, subscription_service, session, i18n_instance, lang)
        
    except Exception as e:
        logging.error(f"Error resetting trial for user {user.user_id}: {e}")
        await session.rollback()
        await callback.answer(_(
            "admin_user_trial_reset_error",
            default="❌ Ошибка сброса триала"
        ), show_alert=True)


async def handle_add_subscription_prompt(callback: types.CallbackQuery, state: FSMContext,
                                       user: User, i18n_instance, lang: str):
    """Prompt admin to enter subscription days to add"""
    _ = lambda key, **kwargs: i18n_instance.gettext(lang, key, **kwargs)
    
    await state.update_data(target_user_id=user.user_id)
    await state.set_state(AdminStates.waiting_for_subscription_days_to_add)
    
    prompt_text = _(
        "admin_user_add_subscription_prompt",
        default="➕ Добавление дней подписки для пользователя {user_id}\n\nВведите количество дней для добавления:",
        user_id=user.user_id
    )
    
    try:
        await callback.message.edit_text(prompt_text)
    except Exception:
        await callback.message.answer(prompt_text)
    
    await callback.answer()


async def handle_toggle_ban(callback: types.CallbackQuery, user: User,
                          panel_service: PanelApiService, session: AsyncSession,
                          i18n_instance, lang: str):
    """Toggle user ban status"""
    _ = lambda key, **kwargs: i18n_instance.gettext(lang, key, **kwargs)
    
    try:
        new_ban_status = not user.is_banned
        
        # Update in database
        await user_dal.update_user(session, user.user_id, {"is_banned": new_ban_status})
        
        # Update on panel if user has panel UUID
        if user.panel_user_uuid:
            panel_status = "DISABLED" if new_ban_status else "ACTIVE"
            await panel_service.update_user_status_on_panel(user.panel_user_uuid, not new_ban_status)
        
        await session.commit()
        
        status_text = _("admin_user_ban_action_banned", default="заблокирован") if new_ban_status else _("admin_user_ban_action_unbanned", default="разблокирован")
        await callback.answer(_(
            "admin_user_ban_toggle_success",
            default="✅ Пользователь {status}",
            status=status_text
        ), show_alert=True)
        
        # Refresh user card with updated ban status
        user.is_banned = new_ban_status  # Update local object
        from config.settings import Settings
        from bot.services.panel_api_service import PanelApiService
        settings = Settings()
        async with PanelApiService(settings) as panel_service:
            subscription_service = SubscriptionService(settings, panel_service)
            await handle_refresh_user_card(callback, user, subscription_service, session, i18n_instance, lang)
        
    except Exception as e:
        logging.error(f"Error toggling ban for user {user.user_id}: {e}")
        await session.rollback()
        await callback.answer(_(
            "admin_user_ban_toggle_error",
            default="❌ Ошибка изменения статуса блокировки"
        ), show_alert=True)


async def handle_send_message_prompt(callback: types.CallbackQuery, state: FSMContext,
                                   user: User, i18n_instance, lang: str):
    """Prompt admin to enter message to send to user"""
    _ = lambda key, **kwargs: i18n_instance.gettext(lang, key, **kwargs)
    
    await state.update_data(target_user_id=user.user_id)
    await state.set_state(AdminStates.waiting_for_direct_message_to_user)
    
    prompt_text = _(
        "admin_user_send_message_prompt",
        default="✉️ Отправка сообщения пользователю {user_id}\n\nВведите текст сообщения:",
        user_id=user.user_id
    )
    
    try:
        await callback.message.edit_text(prompt_text)
    except Exception:
        await callback.message.answer(prompt_text)
    
    await callback.answer()


async def handle_view_user_logs(callback: types.CallbackQuery, user: User,
                              session: AsyncSession, settings: Settings,
                              i18n_instance, lang: str):
    """Show recent user logs"""
    _ = lambda key, **kwargs: i18n_instance.gettext(lang, key, **kwargs)
    
    try:
        # Get recent logs for user
        logs = await message_log_dal.get_user_message_logs(session, user.user_id, limit=10, offset=0)
        
        if not logs:
            await callback.answer(_(
                "admin_user_no_logs",
                default="📜 У пользователя нет действий"
            ), show_alert=True)
            return
        
        logs_text_parts = [
            f"{_('admin_user_recent_actions_title', default='📜 Последние действия пользователя {user_id}:', user_id=user.user_id)}\n"
        ]
        
        for log in logs:
            timestamp = log.timestamp.strftime('%Y-%m-%d %H:%M') if log.timestamp else 'N/A'
            event_type = log.event_type or 'N/A'
            content_preview = (log.content or '')[:50] + ('...' if len(log.content or '') > 50 else '')
            
            logs_text_parts.append(
                f"🕐 {hcode(timestamp)} - {hcode(event_type)}\n"
                f"   {content_preview}"
            )
        
        logs_text = "\n\n".join(logs_text_parts)
        
        # Create inline keyboard for full logs
        builder = InlineKeyboardBuilder()
        builder.button(
            text=_(key="admin_user_view_all_logs_button", default="📋 Все действия"),
            callback_data=f"admin_logs:view_user:{user.user_id}:0"
        )
        builder.button(
            text=_(key="admin_user_back_to_card_button", default="🔙 К карточке"),
            callback_data=f"user_action:refresh:{user.user_id}"
        )
        builder.adjust(1)
        
        try:
            await callback.message.edit_text(
                logs_text,
                reply_markup=builder.as_markup(),
                parse_mode="HTML"
            )
        except Exception:
            await callback.message.answer(
                logs_text,
                reply_markup=builder.as_markup(),
                parse_mode="HTML"
            )
        
        await callback.answer()
        
    except Exception as e:
        logging.error(f"Error viewing logs for user {user.user_id}: {e}")
        await callback.answer(_(
            "admin_user_logs_error",
            default="❌ Ошибка загрузки действий пользователя"
        ), show_alert=True)


async def handle_ip_control_view(
        callback: types.CallbackQuery,
        user: User,
        panel_service: PanelApiService,
        i18n_instance,
        lang: str):
    _ = lambda key, **kwargs: i18n_instance.gettext(lang, key, **kwargs)

    if not callback.message:
        await callback.answer()
        return

    if not user.panel_user_uuid:
        await callback.answer(
            _("admin_user_ip_control_no_panel_uuid",
              default="У пользователя нет panel UUID."),
            show_alert=True,
        )
        return

    await callback.answer(
        _("admin_user_ip_control_loading_toast",
          default="⏳ Запрашиваю IP-данные, это может занять до 20 секунд..."),
        show_alert=False,
    )
    text_parts = [
        _("admin_user_ip_control_header",
          default="🌐 <b>IP-control для пользователя {user_id}</b>",
          user_id=user.user_id),
        "",
        _("admin_user_ip_control_loading",
          default="⏳ Идёт запрос данных по всем нодам..."),
        "",
    ]

    try:
        await callback.message.edit_text(
            "\n".join(text_parts),
            parse_mode="HTML",
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise

    try:
        diagnostics = await panel_service.get_user_ip_diagnostics(
            user_uuid=user.panel_user_uuid,
            max_nodes=None,
            poll_attempts=20,
            poll_delay_seconds=1.0,
        )
        text_parts = [
            _("admin_user_ip_control_header",
              default="🌐 <b>IP-control для пользователя {user_id}</b>",
              user_id=user.user_id),
            "",
        ]
        if not diagnostics:
            text_parts.append(
                _("admin_user_ip_control_empty",
                  default="Данные IP-control пока не найдены.")
            )
        else:
            for node_item in diagnostics:
                node_name = node_item.get("node_name", "Node")
                node_uuid = node_item.get("node_uuid", "")
                text_parts.append(
                    _("admin_user_ip_control_node_title",
                      default="🖥 <b>{node_name}</b> ({node_uuid})",
                      node_name=node_name,
                      node_uuid=node_uuid[:8] + "..." if node_uuid else "n/a")
                )
                ips = node_item.get("ips", [])[:5]
                for idx, ip_item in enumerate(ips, start=1):
                    ip_value = ip_item.get("ip", "N/A")
                    last_seen = ip_item.get("lastSeen", "")
                    if last_seen:
                        text_parts.append(
                            _("admin_user_ip_control_item_seen",
                              default="{index}. <code>{ip}</code> — {last_seen}",
                              index=idx,
                              ip=ip_value,
                              last_seen=last_seen)
                        )
                    else:
                        text_parts.append(
                            _("admin_user_ip_control_item",
                              default="{index}. <code>{ip}</code>",
                              index=idx,
                              ip=ip_value)
                        )
                text_parts.append("")
    except Exception as e:
        logging.error("Failed to render IP-control view for user %s: %s", user.user_id, e, exc_info=True)
        text_parts.append(
            _("admin_user_ip_control_error",
              default="❌ Ошибка загрузки данных IP-control.")
        )

    kb = InlineKeyboardBuilder()
    kb.button(
        text=_("admin_user_ip_control_refresh_button", default="🔄 Обновить IP-control"),
        callback_data=f"user_action:ip_control:{user.user_id}",
    )
    kb.button(
        text=_("admin_user_back_to_card_button", default="🔙 К карточке"),
        callback_data=f"user_action:refresh:{user.user_id}",
    )
    kb.adjust(1, 1)

    try:
        await callback.message.edit_text(
            "\n".join(text_parts),
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
        logging.info(
            "IP-control view for user %s was not changed; skipping message update.",
            user.user_id,
        )


async def handle_refresh_user_card(callback: types.CallbackQuery, user: User,
                                  subscription_service: SubscriptionService,
                                  session: AsyncSession, i18n_instance, lang: str):
    """Refresh user card with latest information"""
    try:
        # Reload user from database
        fresh_user = await user_dal.get_user_by_id(session, user.user_id)
        if not fresh_user:
            await callback.answer("User not found", show_alert=True)
            return
        
        from config.settings import Settings as _Settings
        _settings = _Settings()
        referral_service = ReferralService(_settings, subscription_service, callback.message.bot, i18n_instance)
        user_card_text = await format_user_card(fresh_user, session, subscription_service, i18n_instance, lang, referral_service)
        keyboard = get_user_card_keyboard(
            fresh_user.user_id,
            i18n_instance,
            lang,
            fresh_user.referred_by_id
        )
        markup = keyboard.as_markup()
        
        try:
            await _send_with_profile_link_fallback(
                callback.message.edit_text,
                text=user_card_text,
                markup=markup,
                user_id=fresh_user.user_id,
                parse_mode="HTML"
            )
        except Exception:
            await _send_with_profile_link_fallback(
                callback.message.answer,
                text=user_card_text,
                markup=markup,
                user_id=fresh_user.user_id,
                parse_mode="HTML"
            )
        
        await callback.answer()
        
    except Exception as e:
        logging.error(f"Error refreshing user card for {user.user_id}: {e}")
        await callback.answer("Error refreshing user card", show_alert=True)


# Destructive deletion flow
async def handle_delete_user_prompt(callback: types.CallbackQuery, state: FSMContext,
                                    user: User, settings: Settings, i18n_instance,
                                    lang: str, session: AsyncSession):
    """Trigger confirmation workflow for destructive deletion."""
    _ = lambda key, **kwargs: i18n_instance.gettext(lang, key, **kwargs)

    admin = callback.from_user
    admin_id = admin.id if admin else None
    if not admin_id or admin_id not in settings.ADMIN_IDS:
        logging.warning(
            f"Unauthorized delete attempt by user {admin_id} targeting {user.user_id}."
        )
        await callback.answer(
            _(
                "admin_user_delete_not_allowed",
                default="❌ У вас нет прав для удаления пользователей.",
            ),
            show_alert=True,
        )
        return

    await state.update_data(
        target_user_id=user.user_id,
        delete_initiator_id=admin_id,
    )
    await state.set_state(AdminStates.waiting_for_user_delete_confirmation)

    prompt_text = _(
        "admin_user_delete_confirmation_prompt",
        default=(
            "⚠️ Вы хотите полностью удалить пользователя {user_id}.\n\n"
            "Отправьте точный Telegram ID этого пользователя, чтобы подтвердить удаление.\n"
            "Любой другой ответ отменит операцию."
        ),
        user_id=hcode(str(user.user_id)),
    )

    try:
        await callback.message.answer(prompt_text, parse_mode="HTML")
    except Exception as e:
        logging.error(
            f"Failed to send delete confirmation prompt for user {user.user_id}: {e}"
        )
        await callback.message.reply(prompt_text, parse_mode="HTML")

    await callback.answer()


async def _log_admin_user_deletion(
    session: AsyncSession,
    admin_id: int,
    admin_user: Optional[types.User],
    target_user_id: int,
) -> None:
    """Store audit log for successful deletion."""
    try:
        await message_log_dal.create_message_log_no_commit(
            session,
            {
                "user_id": admin_id,
                "telegram_username": admin_user.username if admin_user else None,
                "telegram_first_name": admin_user.first_name if admin_user else None,
                "event_type": "admin:user_deleted",
                "content": f"Admin {admin_id} deleted user {target_user_id}",
                "raw_update_preview": None,
                "is_admin_event": True,
                "target_user_id": target_user_id,
                "timestamp": datetime.now(timezone.utc),
            },
        )
    except Exception as e:
        logging.error(
            f"Failed to log deletion audit for admin {admin_id} -> user {target_user_id}: {e}",
            exc_info=True,
        )


# Message handlers for state-based inputs

@router.message(AdminStates.waiting_for_user_delete_confirmation, F.text)
async def process_delete_user_confirmation_handler(message: types.Message,
                                                   state: FSMContext,
                                                   settings: Settings,
                                                   i18n_data: dict,
                                                   panel_service: PanelApiService,
                                                   session: AsyncSession):
    """Confirm and execute destructive user deletion."""
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n:
        await message.reply("Language service error.")
        await state.clear()
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    admin = message.from_user
    admin_id = admin.id if admin else None
    if not admin_id or admin_id not in settings.ADMIN_IDS:
        logging.warning(
            f"Unauthorized delete confirmation attempt by user {admin_id}."
        )
        await message.answer(
            _(
                "admin_user_delete_not_allowed",
                default="❌ У вас нет прав для удаления пользователей.",
            )
        )
        await state.clear()
        return

    data = await state.get_data()
    target_user_id = data.get("target_user_id")
    if not target_user_id:
        await message.answer(
            _(
                "admin_user_delete_state_missing",
                default="⚠️ Нет активной операции удаления. Начните заново.",
            )
        )
        await state.clear()
        return

    confirmation_input = message.text.strip() if message.text else ""
    if confirmation_input.lower() in {"/cancel", "cancel", "отмена"}:
        await message.answer(
            _(
                "admin_user_delete_cancelled",
                default="Операция удаления отменена по запросу.",
            )
        )
        await state.clear()
        return

    if confirmation_input != str(target_user_id):
        await message.answer(
            _(
                "admin_user_delete_mismatch",
                default="⚠️ ID не совпадает. Удаление отменено.",
            )
        )
        await state.clear()
        return

    user_model = await user_dal.get_user_by_id(session, target_user_id)
    if not user_model:
        await message.answer(
            _(
                "admin_user_delete_already_removed",
                default="ℹ️ Пользователь уже удален.",
            )
        )
        await state.clear()
        return

    try:
        if user_model.panel_user_uuid:
            panel_deleted = await panel_service.delete_user_from_panel(
                user_model.panel_user_uuid
            )
            if not panel_deleted:
                await message.answer(
                    _(
                        "admin_user_delete_panel_error",
                        default=(
                            "❌ Не удалось удалить пользователя на панели. "
                            "Операция прервана."
                        ),
                    )
                )
                await session.rollback()
                await state.clear()
                return

        deleted = await user_dal.delete_user_and_relations(
            session, target_user_id
        )
        if not deleted:
            await message.answer(
                _(
                    "admin_user_delete_already_removed",
                    default="ℹ️ Пользователь уже удален.",
                )
            )
            await state.clear()
            return

        await _log_admin_user_deletion(session, admin_id, admin, target_user_id)
        await session.commit()

        await message.answer(
            _(
                "admin_user_delete_success",
                default="✅ Пользователь {user_id} удален из бота и панели.",
                user_id=hcode(str(target_user_id)),
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        logging.error(f"Error deleting user {target_user_id}: {e}", exc_info=True)
        await session.rollback()
        await message.answer(
            _(
                "admin_user_delete_error",
                default="❌ Не удалось завершить удаление пользователя. Попробуйте позже.",
            )
        )
    finally:
        await state.clear()


@router.message(AdminStates.waiting_for_subscription_days_to_add, F.text)
async def process_subscription_days_handler(message: types.Message, state: FSMContext,
                                           settings: Settings, i18n_data: dict,
                                           subscription_service: SubscriptionService,
                                           session: AsyncSession):
    """Process subscription days input"""
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n:
        await message.reply("Language service error.")
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    data = await state.get_data()
    target_user_id = data.get("target_user_id")
    if not target_user_id:
        await message.answer("Error: target user not found in state")
        await state.clear()
        return

    try:
        days_to_add = int(message.text.strip())
        if days_to_add <= 0 or days_to_add > 3650:  # Max 10 years
            raise ValueError("Invalid days count")
    except ValueError:
        await message.answer(_(
            "admin_user_invalid_days",
            default="❌ Неверное количество дней. Введите число от 1 до 3650."
        ))
        return

    try:
        # Extend subscription
        result = await subscription_service.extend_active_subscription_days(
            session, target_user_id, days_to_add, "admin_manual_extension"
        )
        
        if result:
            await session.commit()
            await message.answer(_(
                "admin_user_subscription_added_success",
                default="✅ Успешно добавлено {days} дней подписки пользователю {user_id}",
                days=days_to_add,
                user_id=target_user_id
            ))
            
            # Show updated user card
            user = await user_dal.get_user_by_id(session, target_user_id)
            if user:
                referral_service = ReferralService(settings, subscription_service, message.bot, i18n)
                user_card_text = await format_user_card(user, session, subscription_service, i18n, current_lang, referral_service)
                keyboard = get_user_card_keyboard(
                    user.user_id,
                    i18n,
                    current_lang,
                    user.referred_by_id
                )
                
                await _send_with_profile_link_fallback(
                    message.answer,
                    text=user_card_text,
                    markup=keyboard.as_markup(),
                    user_id=user.user_id,
                    parse_mode="HTML"
                )
        else:
            await session.rollback()
            await message.answer(_(
                "admin_user_subscription_added_error",
                default="❌ Ошибка добавления дней подписки"
            ))
    
    except Exception as e:
        logging.error(f"Error adding subscription days for user {target_user_id}: {e}")
        await session.rollback()
        await message.answer(_(
            "admin_user_subscription_added_error",
            default="❌ Ошибка добавления дней подписки"
        ))
    
    await state.clear()


@router.message(AdminStates.waiting_for_direct_message_to_user)
async def process_direct_message_handler(message: types.Message, state: FSMContext,
                                       settings: Settings, i18n_data: dict,
                                       bot: Bot, session: AsyncSession):
    """Process direct message to user"""
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n:
        await message.reply("Language service error.")
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    data = await state.get_data()
    target_user_id = data.get("target_user_id")
    if not target_user_id:
        await message.answer("Error: target user not found in state")
        await state.clear()
        return

    # Determine content similar to broadcast
    text = (message.text or message.caption or "").strip()
    if len(text) > 4000:
        await message.answer(_(
            "admin_user_message_too_long",
            default="❌ Сообщение слишком длинное (максимум 4000 символов)"
        ))
        return

    try:
        # Get target user
        target_user = await user_dal.get_user_by_id(session, target_user_id)
        if not target_user:
            await message.answer("Target user not found")
            await state.clear()
            return

        # Prepare admin signature and get content
        admin_signature = _(
            "admin_direct_message_signature",
            default="\n\n---\n💬 Сообщение от администратора"
        )
        
        content = get_message_content(message)

        if not content.text and not content.file_id:
            await message.answer(_(
                "admin_direct_empty_message",
                default="❌ Пустое сообщение. Отправьте текст или медиа."
            ))
            return

        caption_with_signature = (content.text + admin_signature) if content.text else None

        # Send to target user using our fancy match/case function
        try:
            await send_direct_message(
                bot,
                target_user_id, 
                content,
                extra_text=admin_signature,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except TelegramBadRequest as e:
            await message.answer(_(
                "admin_broadcast_invalid_html",
                default="❌ Некорректный HTML в сообщении. Пожалуйста, отправьте корректный HTML (поддерживаются теги Telegram) или уберите теги.\nОшибка: {error}",
                error=str(e),
            ))
            return
        
        # Confirm to admin
        await message.answer(_(
            "admin_user_message_sent_success",
            default="✅ Сообщение отправлено пользователю {user_id}",
            user_id=target_user_id
        ))
        
        # Show user card again  
        from bot.services.panel_api_service import PanelApiService
        async with PanelApiService(settings) as panel_service:
            subscription_service = SubscriptionService(settings, panel_service)
            referral_service = ReferralService(settings, subscription_service, bot, i18n)
            user_card_text = await format_user_card(target_user, session, subscription_service, i18n, current_lang, referral_service)
            keyboard = get_user_card_keyboard(
                target_user.user_id,
                i18n,
                current_lang,
                target_user.referred_by_id
            )
            
            await _send_with_profile_link_fallback(
                message.answer,
                text=user_card_text,
                markup=keyboard.as_markup(),
                user_id=target_user.user_id,
                parse_mode="HTML"
            )
        
    except Exception as e:
        logging.error(f"Error sending direct message to user {target_user_id}: {e}")
        await message.answer(_(
            "admin_user_message_sent_error",
            default="❌ Ошибка отправки сообщения"
        ))
    
    await state.clear()


async def ban_user_prompt_handler(callback: types.CallbackQuery,
                                 state: FSMContext, i18n_data: dict,
                                 settings: Settings, session: AsyncSession):
    """Prompt admin to enter user ID or username to ban"""
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n or not callback.message:
        await callback.answer("Error preparing ban prompt.", show_alert=True)
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    prompt_text = _(
        "admin_ban_user_prompt",
        default="🚫 Блокировка пользователя\n\nВведите ID пользователя или @username для блокировки:"
    )

    try:
        await callback.message.edit_text(
            prompt_text,
            reply_markup=get_back_to_admin_panel_keyboard(current_lang, i18n)
        )
    except Exception as e:
        logging.warning(f"Could not edit message for ban prompt: {e}. Sending new.")
        await callback.message.answer(
            prompt_text,
            reply_markup=get_back_to_admin_panel_keyboard(current_lang, i18n)
        )
    
    await callback.answer()
    await state.set_state(AdminStates.waiting_for_user_id_to_ban)


async def unban_user_prompt_handler(callback: types.CallbackQuery,
                                   state: FSMContext, i18n_data: dict,
                                   settings: Settings, session: AsyncSession):
    """Prompt admin to enter user ID or username to unban"""
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n or not callback.message:
        await callback.answer("Error preparing unban prompt.", show_alert=True)
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    prompt_text = _(
        "admin_unban_user_prompt",
        default="✅ Разблокировка пользователя\n\nВведите ID пользователя или @username для разблокировки:"
    )

    try:
        await callback.message.edit_text(
            prompt_text,
            reply_markup=get_back_to_admin_panel_keyboard(current_lang, i18n)
        )
    except Exception as e:
        logging.warning(f"Could not edit message for unban prompt: {e}. Sending new.")
        await callback.message.answer(
            prompt_text,
            reply_markup=get_back_to_admin_panel_keyboard(current_lang, i18n)
        )
    
    await callback.answer()
    await state.set_state(AdminStates.waiting_for_user_id_to_unban)


async def view_banned_users_handler(callback: types.CallbackQuery,
                                  state: FSMContext, i18n_data: dict,
                                  settings: Settings, session: AsyncSession,
                                  page: int = 0):
    """Display list of banned users"""
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n or not callback.message:
        await callback.answer("Error preparing banned users list.", show_alert=True)
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    try:
        # Get paginated banned users
        from bot.keyboards.inline.admin_keyboards import get_banned_users_keyboard

        page_size = settings.LOGS_PAGE_SIZE
        banned_users = await user_dal.get_banned_users_paginated(
            session, page=page, page_size=page_size
        )
        total_banned = await user_dal.count_banned_users(session)
        total_pages = max(1, (total_banned + page_size - 1) // page_size)

        if total_banned == 0:
            message_text = _(
                "admin_banned_users_empty",
                default="📋 Заблокированные пользователи\n\nСписок пуст"
            )
            keyboard = get_back_to_admin_panel_keyboard(current_lang, i18n)
        else:
            message_text = _(
                "admin_banned_users_list",
                default="📋 Заблокированные пользователи\n\nСтраница {current}/{total} ({count} пользователей)",
                current=page + 1,
                total=total_pages,
                count=total_banned
            )
            keyboard = get_banned_users_keyboard(
                banned_users=banned_users,
                current_page=page,
                total_banned=total_banned,
                i18n_instance=i18n,
                lang=current_lang,
                settings=settings,
            )

        await callback.message.edit_text(
            message_text,
            reply_markup=keyboard
        )
        
    except Exception as e:
        logging.error(f"Error displaying banned users: {e}")
        await callback.answer("Error loading banned users", show_alert=True)


@router.message(AdminStates.waiting_for_user_id_to_ban, F.text)
async def process_ban_user_handler(message: types.Message, state: FSMContext,
                                  settings: Settings, i18n_data: dict,
                                  panel_service: PanelApiService,
                                  session: AsyncSession):
    """Process user ban input"""
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n:
        await message.reply("Language service error.")
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    input_text = message.text.strip() if message.text else ""
    user_model: Optional[User] = None

    # Try to find user by ID or username
    if input_text.isdigit():
        try:
            user_model = await user_dal.get_user_by_id(session, int(input_text))
        except ValueError:
            pass
    elif input_text.startswith("@") and USERNAME_REGEX.match(input_text[1:]):
        user_model = await user_dal.get_user_by_username(session, input_text[1:])
    elif USERNAME_REGEX.match(input_text):
        user_model = await user_dal.get_user_by_username(session, input_text)

    if not user_model:
        await message.answer(_(
            "admin_user_not_found",
            default="❌ Пользователь не найден: {input}",
            input=hcode(input_text)
        ))
        return

    try:
        # Check if user is already banned
        if user_model.is_banned:
            await message.answer(_(
                "admin_user_already_banned",
                default="⚠️ Пользователь уже заблокирован"
            ))
            await state.clear()
            return

        # Ban the user
        await user_dal.update_user(session, user_model.user_id, {"is_banned": True})
        
        # Update on panel if user has panel UUID
        if user_model.panel_user_uuid:
            await panel_service.update_user_status_on_panel(user_model.panel_user_uuid, False)
        
        await session.commit()
        
        await message.answer(_(
            "admin_user_ban_success",
            default="✅ Пользователь {input} заблокирован",
            input=hcode(input_text)
        ))
        
    except Exception as e:
        logging.error(f"Error banning user {user_model.user_id}: {e}")
        await session.rollback()
        await message.answer(_(
            "admin_user_ban_error",
            default="❌ Ошибка блокировки пользователя"
        ))
    
    await state.clear()


@router.message(AdminStates.waiting_for_user_id_to_unban, F.text)
async def process_unban_user_handler(message: types.Message, state: FSMContext,
                                   settings: Settings, i18n_data: dict,
                                   panel_service: PanelApiService,
                                   session: AsyncSession):
    """Process user unban input"""
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n:
        await message.reply("Language service error.")
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    input_text = message.text.strip() if message.text else ""
    user_model: Optional[User] = None

    # Try to find user by ID or username
    if input_text.isdigit():
        try:
            user_model = await user_dal.get_user_by_id(session, int(input_text))
        except ValueError:
            pass
    elif input_text.startswith("@") and USERNAME_REGEX.match(input_text[1:]):
        user_model = await user_dal.get_user_by_username(session, input_text[1:])
    elif USERNAME_REGEX.match(input_text):
        user_model = await user_dal.get_user_by_username(session, input_text)

    if not user_model:
        await message.answer(_(
            "admin_user_not_found",
            default="❌ Пользователь не найден: {input}",
            input=hcode(input_text)
        ))
        return

    try:
        # Check if user is not banned
        if not user_model.is_banned:
            await message.answer(_(
                "admin_user_not_banned",
                default="⚠️ Пользователь не заблокирован"
            ))
            await state.clear()
            return

        # Unban the user
        await user_dal.update_user(session, user_model.user_id, {"is_banned": False})
        
        # Update on panel if user has panel UUID
        if user_model.panel_user_uuid:
            await panel_service.update_user_status_on_panel(user_model.panel_user_uuid, True)
        
        await session.commit()
        
        await message.answer(_(
            "admin_user_unban_success",
            default="✅ Пользователь {input} разблокирован",
            input=hcode(input_text)
        ))
        
    except Exception as e:
        logging.error(f"Error unbanning user {user_model.user_id}: {e}")
        await session.rollback()
        await message.answer(_(
            "admin_user_unban_error",
            default="❌ Ошибка разблокировки пользователя"
        ))
    
    await state.clear()


@router.callback_query(F.data.startswith("admin_user_card_from_list:"))
async def user_card_from_list_handler(callback: types.CallbackQuery,
                                     state: FSMContext, i18n_data: dict,
                                     settings: Settings, bot: Bot,
                                     subscription_service: SubscriptionService,
                                     panel_service: PanelApiService,
                                     session: AsyncSession):
    """Display user card when clicked from user list"""
    try:
        parts = callback.data.split(":")
        user_id = int(parts[1])
        page = int(parts[2])
    except (IndexError, ValueError):
        await callback.answer("Invalid user data", show_alert=True)
        return
    
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n:
        await callback.answer("Language service error", show_alert=True)
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)
    
    # Get user from database
    user = await user_dal.get_user_by_id(session, user_id)
    if not user:
        await callback.answer("User not found", show_alert=True)
        return
    
    # Create keyboard with back to list button
    keyboard = get_user_card_keyboard(
        user_id,
        i18n,
        current_lang,
        user.referred_by_id
    )
    keyboard.button(
        text=_("admin_user_back_to_list_button", default="⬅️ К списку"),
        callback_data=f"admin_action:users_list:{page}"
    )
    quick_links_width = 2 if user.referred_by_id else 1
    keyboard.adjust(2, 2, 2, quick_links_width, 1, 2, 1)
    
    # Format user card
    try:
        from bot.services.referral_service import ReferralService
        referral_service = ReferralService(settings, subscription_service, bot, i18n)
        user_card_text = await format_user_card(user, session, subscription_service, i18n, current_lang, referral_service)
        markup = keyboard.as_markup()
        
        await _send_with_profile_link_fallback(
            callback.message.edit_text,
            text=user_card_text,
            markup=markup,
            user_id=user.user_id,
            parse_mode="HTML"
        )
        await callback.answer()
        
    except Exception as e:
        logging.error(f"Error displaying user card: {e}")
        await callback.answer("Error displaying user card", show_alert=True)
