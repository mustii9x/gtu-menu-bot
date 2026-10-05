"""Handler smoke tests with lightweight fakes instead of real Telegram updates."""

import asyncio
from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from bot import handlers
from bot.config import Config
from bot.menu import MenuStore

ADMIN = 999


def make_update(chat_id, language_code="tr", text=None, document=None, caption=None, callback_data=None):
    message = SimpleNamespace(
        text=text, caption=caption, document=document, photo=None, reply_text=AsyncMock()
    )
    callback = None
    if callback_data:
        callback = SimpleNamespace(
            data=callback_data, answer=AsyncMock(), edit_message_text=AsyncMock(),
            message=SimpleNamespace(text="⏸ Daily messages paused for all subscribers."),
        )
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(language_code=language_code),
        effective_message=message,
        callback_query=callback,
    )


@pytest.fixture
def context(tmp_path, menu_dir, db, i18n):
    config = Config(
        bot_token="x", admin_chat_id=ADMIN, llm_api_key="", llm_model="m",
        menu_dir=menu_dir, locales_dir=Path("locales"), db_path=tmp_path / "users.db",
        daily_hour=7, daily_minute=30,
    )
    deps = handlers.Deps(config=config, store=MenuStore(menu_dir), db=db, i18n=i18n)
    background = []  # coroutines the handler started with application.create_task
    return SimpleNamespace(
        bot_data={"deps": deps}, user_data={}, args=[], bot=SimpleNamespace(send_message=AsyncMock()),
        application=SimpleNamespace(create_task=lambda coro, update=None: background.append(coro)),
        background=background,
    )


def replies(update):
    return [c.args[0] for c in update.effective_message.reply_text.call_args_list]


def run(coro):
    return asyncio.run(coro)


def test_start_subscribes_and_shows_language_buttons(context, db):
    update = make_update(1, language_code="en-US")
    run(handlers.start(update, context))
    assert db.get_user(1).language == "en"
    texts = replies(update)
    assert "07:30" in texts[0]
    keyboard = update.effective_message.reply_text.call_args_list[1].kwargs["reply_markup"]
    labels = [b.text for b in keyboard.inline_keyboard[0]]
    assert labels == ["🇹🇷 Türkçe", "✅ 🇬🇧 English"]


def test_language_button_changes_language(context, db):
    run(handlers.start(make_update(1, "en"), context))
    update = make_update(1, callback_data="lang:tr")
    run(handlers.language_button(update, context))
    assert db.get_user(1).language == "tr"
    update.callback_query.edit_message_text.assert_awaited_with("Dil Türkçe olarak ayarlandı. 🇹🇷")


def test_menu_today_replies_in_users_language(context, db):
    db.start_user(1, "en", date(2026, 10, 1))
    update = make_update(1)
    with patch("bot.handlers.today_local", return_value=date(2026, 10, 14)):
        run(handlers.menu_today(update, context))
    assert replies(update)[0].startswith("Wednesday, 14 October 2026")


def test_stop_then_feedback_flow(context, db):
    run(handlers.start(make_update(1, "tr"), context))
    run(handlers.stop(make_update(1), context))
    assert not db.get_user(1).subscribed

    run(handlers.feedback(make_update(1), context))
    assert context.user_data["awaiting_feedback"]
    update = make_update(1, text="Pilav çok tuzluydu")
    run(handlers.text_message(update, context))
    sent = context.bot.send_message.call_args.kwargs
    assert sent["chat_id"] == ADMIN and "Feedback (TR)" in sent["text"] and "Pilav" in sent["text"]
    assert "awaiting_feedback" not in context.user_data


def test_admin_commands_are_hidden_from_students(context):
    update = make_update(1, "en")
    run(handlers.stats(update, context))
    assert replies(update)[0].startswith("Commands:")


def test_admin_json_upload_then_approve(context, menu_dir):
    menu = {"2026-12-01": [{"tr": "Mercimek çorbası", "en": "Red lentil soup"}]}
    tg_file = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(json.dumps(menu).encode())))
    document = SimpleNamespace(file_name="2026-12.json", get_file=AsyncMock(return_value=tg_file))

    upload = make_update(ADMIN, document=document)
    run(handlers.admin_json(upload, context))
    assert "Menu preview for December 2026" in replies(upload)[0]
    assert not (menu_dir / "2026-12.json").exists()   # nothing saved before approval

    approve = make_update(ADMIN)
    run(handlers.approve(approve, context))
    assert "Saved 2026-12.json" in replies(approve)[0]
    assert json.loads((menu_dir / "2026-12.json").read_text(encoding="utf-8")) == menu


def test_menu_with_errors_cannot_be_approved(context, menu_dir):
    tg_file = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(b'{"2026-12-01": []}')))
    document = SimpleNamespace(file_name="2026-12.json", get_file=AsyncMock(return_value=tg_file))
    run(handlers.admin_json(make_update(ADMIN, document=document), context))
    approve = make_update(ADMIN)
    run(handlers.approve(approve, context))
    assert "can't be approved" in replies(approve)[0]
    assert not (menu_dir / "2026-12.json").exists()


# --- menu not published yet / pause and resume ---------------------------------


def test_menu_for_unpublished_month_says_not_published(context, db):
    db.start_user(1, "tr", date(2026, 12, 1))
    db.start_user(2, "en", date(2026, 12, 1))
    tr, en = make_update(1), make_update(2)
    with patch("bot.handlers.today_local", return_value=date(2026, 12, 1)):  # no 2026-12.json
        run(handlers.menu_today(tr, context))
        run(handlers.menu_today(en, context))
    assert replies(tr)[0].startswith("📭 Aralık ayının yemek menüsü okul tarafından henüz yayınlanmadı")
    assert replies(en)[0].startswith("📭 GTU hasn't published the December menu yet")


def test_tomorrow_in_unpublished_month_names_that_month(context, db):
    db.start_user(1, "en", date(2026, 11, 1))
    update = make_update(1)
    with patch("bot.handlers.today_local", return_value=date(2026, 11, 30)), \
         patch("bot.handlers.tomorrow_local", return_value=date(2026, 12, 1)):
        run(handlers.menu_tomorrow(update, context))
    assert "December menu" in replies(update)[0]


def test_unpublished_month_while_paused_does_not_alert_admin(context, db):
    db.start_user(1, "en", date(2026, 12, 1))
    db.set_paused(True)
    with patch("bot.handlers.today_local", return_value=date(2026, 12, 1)):
        run(handlers.menu_today(make_update(1), context))
    context.bot.send_message.assert_not_awaited()


def test_students_cannot_pause(context, db):
    update = make_update(1, "en")
    run(handlers.pause(update, context))
    assert not db.is_paused()
    assert replies(update)[0].startswith("Commands:")


def test_pause_then_notify_not_published(context, db):
    db.start_user(1, "tr", date(2026, 12, 1))
    db.start_user(2, "en", date(2026, 12, 1))
    update = make_update(ADMIN)
    with patch("bot.handlers.today_local", return_value=date(2026, 12, 1)):
        run(handlers.pause(update, context))
        assert db.is_paused()
        keyboard = update.effective_message.reply_text.call_args.kwargs["reply_markup"]
        assert "December menu not published" in keyboard.inline_keyboard[0][0].text

        button = make_update(ADMIN, callback_data="admin:notify:not_published")

        async def press_and_finish():
            await handlers.notice_button(button, context)
            await asyncio.gather(*context.background)

        run(press_and_finish())

    sent = {c.kwargs["chat_id"]: c.kwargs["text"] for c in context.bot.send_message.call_args_list}
    assert sent[1].startswith("📭 Aralık ayının")
    assert sent[2].startswith("📭 GTU hasn't published the December menu")
    assert "Notice sent to 2 subscribers" in button.callback_query.edit_message_text.call_args.args[0]


def test_pause_without_notice(context, db):
    db.start_user(1, "tr", date(2026, 10, 1))
    run(handlers.pause(make_update(ADMIN), context))
    run(handlers.notice_button(make_update(ADMIN, callback_data="admin:notify:none"), context))
    context.bot.send_message.assert_not_awaited()


def test_resume_turns_messages_back_on(context, db):
    db.set_paused(True)
    update = make_update(ADMIN)
    with patch("bot.handlers.today_local", return_value=date(2026, 10, 14)):
        run(handlers.resume(update, context))
    assert not db.is_paused()
    assert replies(update)[0].startswith("▶️ Daily messages are back on.")


def test_resume_warns_if_menu_still_missing(context, db):
    db.set_paused(True)
    update = make_update(ADMIN)
    with patch("bot.handlers.today_local", return_value=date(2026, 12, 1)):
        run(handlers.resume(update, context))
    assert "still no menu file for 2026-12" in replies(update)[0]


def test_approve_while_paused_reminds_admin(context, db, menu_dir):
    db.set_paused(True)
    menu = {"2026-12-01": [{"tr": "Mercimek çorbası", "en": "Red lentil soup"}]}
    tg_file = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(json.dumps(menu).encode())))
    document = SimpleNamespace(file_name="2026-12.json", get_file=AsyncMock(return_value=tg_file))
    run(handlers.admin_json(make_update(ADMIN, document=document), context))
    approve = make_update(ADMIN)
    run(handlers.approve(approve, context))
    assert "/resume" in replies(approve)[1]
