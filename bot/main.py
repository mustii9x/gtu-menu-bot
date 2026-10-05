"""Starts the bot: loads settings, registers handlers and schedules the daily job.

Run with:  python -m bot.main
"""

from __future__ import annotations

import asyncio
import logging
from datetime import time

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import handlers
from .broadcast import notify_admin, send_daily_menu
from .config import TIMEZONE, Config, load_config
from .db import UserDB
from .i18n import I18n
from .menu import MenuStore, today_local

logger = logging.getLogger("bot")

USER_COMMANDS = handlers.USER_COMMANDS


def setup_logging() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
    )
    # httpx logs every request URL, which contains the bot token. Keep it quiet.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


async def daily_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = handlers.get_deps(context)
    await send_daily_menu(
        context.bot, deps.store, deps.db, deps.i18n, deps.config.admin_chat_id, today_local()
    )


async def post_init(app: Application) -> None:
    deps: handlers.Deps = app.bot_data["deps"]
    config = deps.config

    # Command menu in both languages (Telegram shows the one matching the app language)
    for lang, code in (("en", None), ("tr", "tr")):
        commands = [BotCommand(c, deps.i18n.t(lang, f"cmd_{c}")) for c in USER_COMMANDS]
        await app.bot.set_my_commands(commands, language_code=code)
    await handlers.set_admin_commands(app.bot, deps)

    # Crash detection: the marker file is removed on a clean shutdown.
    marker = config.db_path.parent / ".running"
    marker.parent.mkdir(parents=True, exist_ok=True)
    if marker.exists():
        logger.warning("Previous run did not shut down cleanly")
        await notify_admin(app.bot, config.admin_chat_id, "⚠️ The bot restarted after a crash. Check the logs.")
    marker.touch()
    logger.info(
        "Bot started. Daily menu at %02d:%02d Europe/Istanbul", config.daily_hour, config.daily_minute
    )


async def post_shutdown(app: Application) -> None:
    deps: handlers.Deps = app.bot_data["deps"]
    (deps.config.db_path.parent / ".running").unlink(missing_ok=True)
    deps.db.close()
    logger.info("Bot stopped cleanly")


def build_application(config: Config) -> Application:
    app = (
        Application.builder()
        .token(config.bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.bot_data["deps"] = handlers.Deps(
        config=config,
        store=MenuStore(config.menu_dir),
        db=UserDB(config.db_path),
        i18n=I18n(config.locales_dir),
    )

    private = filters.ChatType.PRIVATE
    admin = filters.Chat(chat_id=config.admin_chat_id)

    for name, callback in [
        ("start", handlers.start),
        ("menu", handlers.menu_today),
        ("tomorrow", handlers.menu_tomorrow),
        ("language", handlers.language),
        ("stop", handlers.stop),
        ("privacy", handlers.privacy),
        ("feedback", handlers.feedback),
        ("help", handlers.help_command),
        # admin commands check the chat ID themselves and show /help to everyone else
        ("stats", handlers.stats),
        ("approve", handlers.approve),
        ("reject", handlers.reject),
        ("pause", handlers.pause),
        ("resume", handlers.resume),
    ]:
        app.add_handler(CommandHandler(name, callback, filters=private))

    app.add_handler(CallbackQueryHandler(handlers.language_button, pattern=r"^lang:"))
    app.add_handler(CallbackQueryHandler(handlers.notice_button, pattern=r"^admin:notify:"))

    # Admin uploads: menu picture (photo or image file) and hand-fixed JSON
    app.add_handler(MessageHandler(private & admin & (filters.PHOTO | filters.Document.IMAGE), handlers.admin_image))
    app.add_handler(
        MessageHandler(
            private & admin & (filters.Document.FileExtension("json") | filters.Document.MimeType("application/json")),
            handlers.admin_json,
        )
    )

    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, handlers.text_message))
    app.add_handler(MessageHandler(private & filters.COMMAND, handlers.help_command))  # unknown commands
    app.add_handler(MessageHandler(private & ~filters.COMMAND & ~filters.TEXT, handlers.other_message))

    app.add_error_handler(handlers.error_handler)

    app.job_queue.run_daily(
        daily_job,
        time=time(config.daily_hour, config.daily_minute, tzinfo=TIMEZONE),
        name="daily-menu",
    )
    return app


def main() -> None:
    setup_logging()
    config = load_config()
    app = build_application(config)
    # Python 3.14 no longer creates an event loop automatically, and
    # python-telegram-bot 21 expects one to exist, so create it here.
    asyncio.set_event_loop(asyncio.new_event_loop())
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
