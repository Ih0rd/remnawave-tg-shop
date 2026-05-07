from pathlib import Path
from typing import Optional

from aiogram import types
from aiogram.types import FSInputFile, InlineKeyboardMarkup

from config.settings import Settings

_ALLOWED_EXTENSIONS = ("jpg", "jpeg", "png", "gif", "webp")


def _resolve_banners_dir(settings: Settings) -> Path:
    custom = getattr(settings, "BANNERS_DIR", None)
    return Path(custom) if custom else Path("../banners")


def find_banner_path(settings: Settings, menu_key: str) -> Optional[Path]:
    banners_dir = _resolve_banners_dir(settings)
    for ext in _ALLOWED_EXTENSIONS:
        candidate = banners_dir / f"{menu_key}.{ext}"
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


async def send_menu_with_optional_banner(
    target_message_obj: types.Message,
    settings: Settings,
    menu_key: str,
    text: str,
    reply_markup: InlineKeyboardMarkup,
    is_edit: bool = False,
):
    banner_path = find_banner_path(settings, menu_key)
    if banner_path:
        if is_edit:
            await target_message_obj.delete()
        await target_message_obj.answer_photo(
            photo=FSInputFile(str(banner_path)),
            caption=text,
            reply_markup=reply_markup,
        )
    else:
        if is_edit:
            await target_message_obj.edit_text(text, reply_markup=reply_markup)
        else:
            await target_message_obj.answer(text, reply_markup=reply_markup)
