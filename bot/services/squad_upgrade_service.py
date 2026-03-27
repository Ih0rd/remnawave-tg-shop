from __future__ import annotations

from datetime import datetime, timedelta, timezone
from calendar import monthrange
from typing import Optional, Dict, Any
import json
import logging

from aiogram import Bot
from aiogram.types import LabeledPrice
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import Settings
from db.dal import user_dal, user_squad_upgrade_dal, payment_dal
from bot.services.panel_api_service import PanelApiService


def _add_months_utc(dt: datetime, months: int) -> datetime:
    base = dt.astimezone(timezone.utc)
    month = base.month - 1 + months
    year = base.year + month // 12
    month = month % 12 + 1
    day = min(base.day, monthrange(year, month)[1])
    return base.replace(year=year, month=month, day=day)


class SquadUpgradeService:
    def __init__(self, settings: Settings, panel_service: PanelApiService, bot: Bot, i18n):
        self.settings = settings
        self.panel_service = panel_service
        self.bot = bot
        self.i18n = i18n

    def _default_squads(self) -> list[str]:
        return list(self.settings.parsed_user_squad_uuids or [])

    async def _update_panel_squads(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        internal_squads: list[str],
    ) -> bool:
        db_user = await user_dal.get_user_by_id(session, user_id)
        if not db_user or not db_user.panel_user_uuid:
            return False
        payload: Dict[str, Any] = {"activeInternalSquads": internal_squads}
        if self.settings.parsed_user_external_squad_uuid:
            payload["externalSquadUuid"] = self.settings.parsed_user_external_squad_uuid
        result = await self.panel_service.update_user_details_on_panel(db_user.panel_user_uuid, payload)
        return bool(result)

    async def activate_paid_upgrade(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        months: int | None = None,
        duration_days: int | None = None,
        provider: str,
        payment_id: int | None = None,
    ) -> Optional[datetime]:
        offer = self.settings.squad_upgrade_offer
        if not offer:
            return None
        if months is None and duration_days is None:
            return None
        target_uuid = str(offer["target_uuid"])
        now = datetime.now(timezone.utc)
        if duration_days is not None:
            expires_at = now + timedelta(days=duration_days)
            months_to_store = 0
        else:
            expires_at = _add_months_utc(now, months or 0)
            months_to_store = months or 0
        from_squads = self._default_squads()
        applied = await self._update_panel_squads(session, user_id=user_id, internal_squads=[target_uuid])
        if not applied:
            return None
        await user_squad_upgrade_dal.create_upgrade(
            session,
            user_id=user_id,
            months=months_to_store,
            provider=provider,
            payment_id=payment_id,
            from_squads=json.dumps(from_squads, ensure_ascii=False),
            to_squad_uuid=target_uuid,
            expires_at=expires_at,
        )
        return expires_at

    async def sync_expired_upgrades(self, session: AsyncSession, user_id: int) -> int:
        expired = await user_squad_upgrade_dal.get_expired_unhandled_upgrades(session, user_id)
        if not expired:
            return 0
        for upgrade in expired:
            upgrade.is_active = False
            upgrade.expiry_notified_at = datetime.now(timezone.utc)
        active_upgrades = await user_squad_upgrade_dal.get_active_upgrades(session, user_id)
        if active_upgrades:
            target_uuid = active_upgrades[-1].to_squad_uuid
            await self._update_panel_squads(session, user_id=user_id, internal_squads=[target_uuid])
        else:
            await self._update_panel_squads(session, user_id=user_id, internal_squads=self._default_squads())
        await session.flush()
        return len(expired)

    async def process_expired_upgrades_and_notify(self, session: AsyncSession, user_id: int) -> int:
        expired_count = await self.sync_expired_upgrades(session, user_id)
        if expired_count <= 0:
            return 0
        await session.commit()
        db_user = await user_dal.get_user_by_id(session, user_id)
        lang = self.settings.DEFAULT_LANGUAGE
        if db_user and db_user.language_code:
            lang = db_user.language_code
        if self.i18n:
            try:
                text = self.i18n.gettext(lang, "squad_upgrade_expired")
            except Exception:
                text = "Your squad upgrade has expired."
        else:
            text = "Your squad upgrade has expired."
        try:
            await self.bot.send_message(user_id, text)
        except Exception as e:
            logging.error(f"Failed to send squad upgrade expiry notification to user {user_id}: {e}")
        return expired_count

    async def create_stars_invoice(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        months: int,
        stars_price: int,
        description: str,
    ) -> Optional[int]:
        payment_record_data = {
            "user_id": user_id,
            "amount": float(stars_price),
            "currency": "XTR",
            "status": "pending_stars",
            "description": description,
            "subscription_duration_months": months,
            "provider": "telegram_stars",
        }
        record = await payment_dal.create_payment_record(session, payment_record_data)
        await session.flush()
        payload = f"upgrade:{record.payment_id}:{months}"
        await self.bot.send_invoice(
            chat_id=user_id,
            title=description,
            description=description,
            payload=payload,
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label=description, amount=stars_price)],
        )
        return record.payment_id
