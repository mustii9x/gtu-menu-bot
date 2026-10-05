"""The daily broadcast, tested with a fake bot (no network)."""

import asyncio
from datetime import date

from telegram.error import Forbidden, NetworkError, RetryAfter

from bot.broadcast import send_daily_menu, send_notice

ADMIN = 999
WEDNESDAY = date(2026, 10, 14)


class FakeBot:
    def __init__(self, errors=None):
        self.errors = errors or {}   # chat_id -> list of exceptions to raise, in order
        self.sent = []

    async def send_message(self, chat_id, text):
        queue = self.errors.get(chat_id)
        if queue:
            raise queue.pop(0)
        self.sent.append((chat_id, text))


def run(coro):
    return asyncio.run(coro)


def test_sends_each_user_their_language(store, db, i18n):
    db.start_user(1, "tr", WEDNESDAY)
    db.start_user(2, "en", WEDNESDAY)
    bot = FakeBot()
    result = run(send_daily_menu(bot, store, db, i18n, ADMIN, WEDNESDAY, delay=0))
    assert result.sent == 2
    texts = dict(bot.sent)
    assert texts[1].startswith("14 Ekim 2026, Çarşamba")
    assert texts[2].startswith("Wednesday, 14 October 2026")


def test_unsubscribed_users_get_nothing(store, db, i18n):
    db.start_user(1, "tr", WEDNESDAY)
    db.set_subscribed(1, False)
    bot = FakeBot()
    run(send_daily_menu(bot, store, db, i18n, ADMIN, WEDNESDAY, delay=0))
    assert bot.sent == []


def test_no_menu_today_sends_nothing(store, db, i18n):
    db.start_user(1, "tr", WEDNESDAY)
    bot = FakeBot()
    result = run(send_daily_menu(bot, store, db, i18n, ADMIN, date(2026, 10, 17), delay=0))
    assert bot.sent == [] and result.skipped_reason == "no menu today"


def test_missing_month_file_on_weekday_alerts_admin(store, db, i18n):
    db.start_user(1, "tr", WEDNESDAY)
    bot = FakeBot()
    run(send_daily_menu(bot, store, db, i18n, ADMIN, date(2026, 12, 2), delay=0))
    assert [c for c, _ in bot.sent] == [ADMIN]
    assert "No usable menu for 2026-12" in bot.sent[0][1]


def test_missing_month_file_on_weekend_is_quiet(store, db, i18n):
    bot = FakeBot()
    run(send_daily_menu(bot, store, db, i18n, ADMIN, date(2026, 12, 5), delay=0))  # Saturday
    assert bot.sent == []


def test_blocked_user_is_unsubscribed_and_others_still_get_it(store, db, i18n):
    for chat_id in (1, 2, 3):
        db.start_user(chat_id, "tr", WEDNESDAY)
    bot = FakeBot({2: [Forbidden("bot was blocked by the user")]})
    result = run(send_daily_menu(bot, store, db, i18n, ADMIN, WEDNESDAY, delay=0))
    assert (result.sent, result.blocked, result.failed) == (2, 1, 0)
    assert not db.get_user(2).subscribed
    assert [c for c, _ in bot.sent] == [1, 3]


def test_rate_limit_waits_and_retries(store, db, i18n):
    db.start_user(1, "en", WEDNESDAY)
    bot = FakeBot({1: [RetryAfter(0)]})
    result = run(send_daily_menu(bot, store, db, i18n, ADMIN, WEDNESDAY, delay=0))
    assert result.sent == 1


def test_many_failures_alert_admin(store, db, i18n):
    for chat_id in range(1, 11):
        db.start_user(chat_id, "tr", WEDNESDAY)
    bot = FakeBot({1: [NetworkError("x")], 2: [NetworkError("y")]})  # 20% fail
    result = run(send_daily_menu(bot, store, db, i18n, ADMIN, WEDNESDAY, delay=0))
    assert (result.sent, result.failed) == (8, 2)
    assert bot.sent[-1][0] == ADMIN and "2 of 10" in bot.sent[-1][1]


def test_paused_sends_nothing_and_does_not_alert(store, db, i18n):
    db.start_user(1, "tr", WEDNESDAY)
    db.set_paused(True)
    bot = FakeBot()
    result = run(send_daily_menu(bot, store, db, i18n, ADMIN, WEDNESDAY, delay=0))
    assert bot.sent == [] and result.skipped_reason == "paused"
    # even with the month file missing on a weekday, a pause means no alert
    run(send_daily_menu(bot, store, db, i18n, ADMIN, date(2026, 12, 2), delay=0))
    assert bot.sent == []


def test_notice_goes_to_each_subscriber_in_their_language(db, i18n):
    db.start_user(1, "tr", WEDNESDAY)
    db.start_user(2, "en", WEDNESDAY)
    db.start_user(3, "en", WEDNESDAY)
    db.set_subscribed(3, False)
    bot = FakeBot({2: [Forbidden("blocked")]})
    result = run(send_notice(bot, db, i18n, "notice_not_published", delay=0,
                             month={"tr": "Kasım", "en": "November"}))
    assert (result.sent, result.blocked) == (1, 1)
    assert bot.sent == [(1, i18n.t("tr", "notice_not_published", month="Kasım"))]
    assert "Kasım ayının" in bot.sent[0][1]
    assert not db.get_user(2).subscribed
