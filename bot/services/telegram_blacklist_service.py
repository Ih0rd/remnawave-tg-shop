import logging
from dataclasses import dataclass
from typing import List, Optional, Set

import aiohttp
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import Settings
from db.dal import user_dal
from .panel_api_service import PanelApiService


@dataclass
class TelegramBlacklistSyncResult:
    fetched_ids: int = 0
    banned_users: int = 0
    users_created: int = 0
    already_banned: int = 0
    panel_disabled: int = 0
    errors: int = 0


class TelegramBlacklistService:
    """Sync Telegram IDs from external plaintext blacklist and ban matching users."""

    def __init__(
        self,
        settings: Settings,
        panel_service: PanelApiService,
        source_url: Optional[str] = None,
    ) -> None:
        self.settings = settings
        self.panel_service = panel_service
        self.source_url = source_url or settings.TELEGRAM_BLACKLIST_SOURCE_URL

    async def sync_blacklist(self, session: AsyncSession) -> TelegramBlacklistSyncResult:
        result = TelegramBlacklistSyncResult()

        if not self.source_url:
            logging.warning("Telegram blacklist sync skipped: source URL is empty.")
            return result

        telegram_ids = await self._fetch_blacklist_ids()
        result.fetched_ids = len(telegram_ids)

        for tg_id in telegram_ids:
            try:
                user = await user_dal.get_user_by_id(session, tg_id)

                if user is None:
                    user_data = {
                        "user_id": tg_id,
                        "username": None,
                        "first_name": None,
                        "last_name": None,
                        "language_code": self.settings.DEFAULT_LANGUAGE,
                        "panel_user_uuid": None,
                        "is_banned": True,
                        "referred_by_id": None,
                    }
                    user, created = await user_dal.create_user(session, user_data)
                    if created:
                        result.users_created += 1

                if not user:
                    result.errors += 1
                    continue

                if user.is_banned:
                    result.already_banned += 1
                else:
                    await user_dal.update_user(session, user.user_id, {"is_banned": True})
                    result.banned_users += 1

                if user.panel_user_uuid:
                    try:
                        await self.panel_service.update_user_status_on_panel(user.panel_user_uuid, False)
                        result.panel_disabled += 1
                    except Exception as panel_error:
                        result.errors += 1
                        logging.error(
                            "Failed to disable panel user %s for tg_id=%s: %s",
                            user.panel_user_uuid,
                            tg_id,
                            panel_error,
                        )
            except Exception as e:
                result.errors += 1
                logging.error("Telegram blacklist sync failed for tg_id=%s: %s", tg_id, e, exc_info=True)

        return result

    async def _fetch_blacklist_ids(self) -> List[int]:
        timeout = aiohttp.ClientTimeout(total=20)

        async with aiohttp.ClientSession(timeout=timeout) as client:
            async with client.get(self.source_url) as response:
                response.raise_for_status()
                raw_content = await response.text()

        ids: Set[int] = set()
        for line in raw_content.splitlines():
            cleaned = line.strip()
            if not cleaned:
                continue
            if "#" in cleaned:
                cleaned = cleaned.split("#", 1)[0].strip()
            if not cleaned:
                continue
            if cleaned.isdigit():
                ids.add(int(cleaned))

        return sorted(ids)
