"""The 07:30 daily broadcast: send today's menu to every subscriber in their language.

Each send has its own error handling, so one failure never stops the rest.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, timedelta

from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

from .db import UserDB
from .i18n import I18n
from .menu import MenuStore, MenuUnavailable, format_menu

logger = logging.getLogger(__name__)

FAILURE_ALERT_RATIO = 0.10
SEND_DELAY_SECONDS = 0.05   # ~20 messages/second, under Telegram's broadcast limit
MAX_RETRIES = 3


@dataclass
class BroadcastResult:
    sent: int = 0
    failed: int = 0
    blocked: int = 0
    skipped_reason: str | None = None

    @property
    def attempted(self) -> int:
        return self.sent + self.failed + self.blocked


def _retry_seconds(exc: RetryAfter) -> float:
    value = exc.retry_after
    return value.total_seconds() if isinstance(value, timedelta) else float(value)


async def _send_one(bot, chat_id: int, text: str) -> str:
    """Send one message. Returns 'sent', 'blocked' or 'failed'."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            await bot.send_message(chat_id=chat_id, text=text)
            return "sent"
        except RetryAfter as exc:
            wait = _retry_seconds(exc)
            logger.warning("Rate limited, waiting %.0fs (attempt %d)", wait, attempt)
            await asyncio.sleep(wait)
        except Forbidden:
            return "blocked"          # user blocked the bot
        except BadRequest as exc:
            if "chat not found" in str(exc).lower():
                return "blocked"      # account deleted
            logger.error("Send to chat %s failed: %s", chat_id, exc)
            return "failed"
        except TelegramError as exc:
            logger.error("Send to chat %s failed: %s", chat_id, exc)
            return "failed"
    logger.error("Send to chat %s failed: still rate limited after %d tries", chat_id, MAX_RETRIES)
    return "failed"


async def send_daily_menu(
    bot,
    store: MenuStore,
    db: UserDB,
    i18n: I18n,
    admin_chat_id: int,
    today: date,
    delay: float = SEND_DELAY_SECONDS,
) -> BroadcastResult:
    result = BroadcastResult()

    if db.is_paused():
        result.skipped_reason = "paused"
        logger.info("Daily menu skipped: paused by admin")
        return result

    try:
        month = store.load_month(today.year, today.month)
    except MenuUnavailable as exc:
        result.skipped_reason = exc.reason
        if today.weekday() < 5:  # weekends normally have no menu, so only alert on weekdays
            await notify_admin(
                bot, admin_chat_id,
                f"⚠️ No usable menu for {today:%Y-%m} ({exc.reason}). "
                "Nothing was sent today. Send the month's menu photo to fix it, "
                "or /pause until GTU publishes it.",
            )
        logger.warning("Daily menu skipped: %s", exc)
        return result

    dishes = month.get(today.isoformat())
    if not dishes:
        result.skipped_reason = "no menu today"
        logger.info("No menu for %s, nothing sent", today)
        return result

    texts = {lang: format_menu(today, dishes, lang, i18n) for lang in ("tr", "en")}
    result = await broadcast_texts(bot, db, texts, delay)

    logger.info(
        "Daily menu sent: %d sent, %d failed, %d blocked", result.sent, result.failed, result.blocked
    )
    if result.attempted and result.failed / result.attempted > FAILURE_ALERT_RATIO:
        await notify_admin(
            bot, admin_chat_id,
            f"⚠️ Daily broadcast: {result.failed} of {result.attempted} sends failed. Check the logs.",
        )
    return result


async def broadcast_texts(
    bot, db: UserDB, texts: dict[str, str], delay: float = SEND_DELAY_SECONDS
) -> BroadcastResult:
    """Send every subscriber the text for their language ({"tr": ..., "en": ...})."""
    result = BroadcastResult()
    for chat_id, lang in db.subscribers():
        outcome = await _send_one(bot, chat_id, texts.get(lang, texts["tr"]))
        if outcome == "sent":
            result.sent += 1
        elif outcome == "blocked":
            result.blocked += 1
            db.set_subscribed(chat_id, False)
            logger.info("Chat %s blocked the bot, unsubscribed", chat_id)
        else:
            result.failed += 1
        if delay:
            await asyncio.sleep(delay)
    return result


async def send_notice(
    bot, db: UserDB, i18n: I18n, key: str, delay: float = SEND_DELAY_SECONDS, **kwargs
) -> BroadcastResult:
    """Send a locale text (e.g. 'notice_not_published') to all subscribers in their language.

    kwargs are filled into the text. A kwarg given as a dict {"tr": ..., "en": ...}
    is filled per language (used for month names).
    """
    texts = {}
    for lang in ("tr", "en"):
        values = {k: (v[lang] if isinstance(v, dict) else v) for k, v in kwargs.items()}
        texts[lang] = i18n.t(lang, key, **values)
    result = await broadcast_texts(bot, db, texts, delay)
    logger.info("Notice %r sent: %d sent, %d failed, %d blocked", key, result.sent, result.failed, result.blocked)
    return result


async def notify_admin(bot, admin_chat_id: int, text: str) -> None:
    """Best-effort alert; an alert failure is logged but never raised."""
    try:
        await bot.send_message(chat_id=admin_chat_id, text=text)
    except TelegramError as exc:
        logger.error("Could not alert admin: %s", exc)
