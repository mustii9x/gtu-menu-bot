"""Telegram command handlers for students and the admin.

Shared objects (config, menu store, database, texts, counters) are kept in
`context.bot_data["deps"]`, set up once in main.py.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from telegram import BotCommand, BotCommandScopeChat, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from . import pipeline
from .broadcast import notify_admin, send_notice
from .config import Config
from .db import RequestCounter, UserDB
from .i18n import I18n, detect_language
from .menu import MenuStore, MenuUnavailable, format_menu, today_local, tomorrow_local

logger = logging.getLogger(__name__)


@dataclass
class PendingMenu:
    year: int
    month: int
    result: pipeline.ValidationResult
    image: bytes | None
    image_ext: str = ".jpg"


@dataclass
class Deps:
    config: Config
    store: MenuStore
    db: UserDB
    i18n: I18n
    counter: RequestCounter = field(default_factory=RequestCounter)
    pending: PendingMenu | None = None
    alerted: set[tuple[date, str]] = field(default_factory=set)


def get_deps(context: ContextTypes.DEFAULT_TYPE) -> Deps:
    return context.bot_data["deps"]


def user_language(update: Update, deps: Deps) -> str:
    chat = update.effective_chat
    user = deps.db.get_user(chat.id) if chat else None
    if user:
        return user.language
    tg_user = update.effective_user
    return detect_language(tg_user.language_code if tg_user else None)


def language_keyboard(selected: str) -> InlineKeyboardMarkup:
    def label(code: str, text: str) -> str:
        return f"✅ {text}" if code == selected else text

    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(label("tr", "🇹🇷 Türkçe"), callback_data="lang:tr"),
            InlineKeyboardButton(label("en", "🇬🇧 English"), callback_data="lang:en"),
        ]]
    )


def is_admin(update: Update, deps: Deps) -> bool:
    chat = update.effective_chat
    return chat is not None and chat.id == deps.config.admin_chat_id


USER_COMMANDS = ["start", "menu", "tomorrow", "language", "stop", "privacy", "feedback", "help"]


async def set_admin_commands(bot, deps: Deps) -> bool:
    """Show the admin-only commands in the admin's command menu.

    Telegram answers "Chat not found" until the admin has pressed Start on the bot,
    so this never raises: it logs a warning and returns False instead.
    """
    commands = [BotCommand(c, deps.i18n.t("en", f"cmd_{c}")) for c in USER_COMMANDS] + [
        BotCommand("stats", "Usage numbers"),
        BotCommand("approve", "Publish the pending menu"),
        BotCommand("reject", "Discard the pending menu"),
        BotCommand("pause", "Pause the daily message for everyone"),
        BotCommand("resume", "Turn the daily message back on"),
    ]
    try:
        await bot.set_my_commands(commands, scope=BotCommandScopeChat(deps.config.admin_chat_id))
        return True
    except TelegramError as exc:
        logger.warning(
            "Could not set the admin command menu (%s). Press Start on the bot from the "
            "admin account, or check ADMIN_CHAT_ID in .env.", exc
        )
        return False


# --- student commands ---------------------------------------------------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = get_deps(context)
    detected = detect_language(update.effective_user.language_code if update.effective_user else None)
    user, _ = deps.db.start_user(update.effective_chat.id, detected, today_local())
    lang = user.language
    time = f"{deps.config.daily_hour:02d}:{deps.config.daily_minute:02d}"
    await update.effective_message.reply_text(deps.i18n.t(lang, "welcome", time=time))
    await update.effective_message.reply_text(
        deps.i18n.t(lang, "choose_language"), reply_markup=language_keyboard(lang)
    )
    if is_admin(update, deps):
        await set_admin_commands(context.bot, deps)


async def language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = get_deps(context)
    lang = user_language(update, deps)
    if deps.db.get_user(update.effective_chat.id) is None:
        await update.effective_message.reply_text(deps.i18n.t(lang, "not_started"))
        return
    await update.effective_message.reply_text(
        deps.i18n.t(lang, "choose_language"), reply_markup=language_keyboard(lang)
    )


async def language_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = get_deps(context)
    query = update.callback_query
    await query.answer()
    lang = (query.data or "").removeprefix("lang:")
    if lang not in ("tr", "en"):
        return
    chat_id = update.effective_chat.id
    if deps.db.get_user(chat_id) is None:
        deps.db.start_user(chat_id, lang, today_local())
    deps.db.set_language(chat_id, lang)
    await query.edit_message_text(deps.i18n.t(lang, "language_set"))


class NotPublished(Exception):
    """The requested day's month has no menu file yet (GTU hasn't released it)."""

    def __init__(self, day: date):
        self.day = day


async def _lookup(context: ContextTypes.DEFAULT_TYPE, day: date) -> list | None:
    """Find a day's dishes, or None if there's no menu that day.

    Raises NotPublished when the whole month's file doesn't exist yet. A missing
    (unless paused) or broken file on a weekday alerts the admin once per day.
    """
    deps = get_deps(context)
    try:
        month = deps.store.load_month(day.year, day.month)
    except MenuUnavailable as exc:
        admin_knows = exc.missing and deps.db.is_paused()
        key = (today_local(), f"{day:%Y-%m}:{exc.reason}")
        if day.weekday() < 5 and not admin_knows and key not in deps.alerted:
            deps.alerted.add(key)
            await notify_admin(
                context.bot, deps.config.admin_chat_id,
                f"⚠️ A student asked for the {day:%Y-%m-%d} menu but it is unavailable: {exc.reason}",
            )
        if exc.missing:
            raise NotPublished(day) from exc
        return None
    return month.get(day.isoformat()) or None


def month_names(deps: Deps, month: int) -> dict[str, str]:
    """{"tr": "Ekim", "en": "October"} for filling {month} in notices."""
    return {lang: deps.i18n.get(lang, "months")[month - 1] for lang in ("tr", "en")}


async def _send_menu_for(update: Update, context: ContextTypes.DEFAULT_TYPE, day: date, command: str) -> None:
    deps = get_deps(context)
    deps.counter.add(command, today_local())
    lang = user_language(update, deps)
    try:
        dishes = await _lookup(context, day)
    except NotPublished:
        text = deps.i18n.t(lang, "menu_not_published", month=month_names(deps, day.month)[lang])
    else:
        if dishes:
            text = format_menu(day, dishes, lang, deps.i18n)
        else:
            text = deps.i18n.t(lang, "no_menu_today" if command == "menu" else "no_menu_tomorrow")
    await update.effective_message.reply_text(text)


async def menu_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_menu_for(update, context, today_local(), "menu")


async def menu_tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_menu_for(update, context, tomorrow_local(), "tomorrow")


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = get_deps(context)
    deps.db.set_subscribed(update.effective_chat.id, False)
    await update.effective_message.reply_text(deps.i18n.t(user_language(update, deps), "stopped"))


async def privacy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = get_deps(context)
    await update.effective_message.reply_text(deps.i18n.t(user_language(update, deps), "privacy"))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = get_deps(context)
    await update.effective_message.reply_text(deps.i18n.t(user_language(update, deps), "help"))


async def feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = get_deps(context)
    lang = user_language(update, deps)
    if context.args:  # "/feedback some text" sends right away
        await _forward_feedback(update, context, " ".join(context.args), lang)
        return
    context.user_data["awaiting_feedback"] = True
    await update.effective_message.reply_text(deps.i18n.t(lang, "feedback_prompt"))


async def _forward_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, lang: str) -> None:
    deps = get_deps(context)
    # Forwarded to the admin, never stored. No name or username is included.
    await context.bot.send_message(
        chat_id=deps.config.admin_chat_id, text=f"📝 Feedback ({lang.upper()}):\n\n{text}"
    )
    await update.effective_message.reply_text(deps.i18n.t(lang, "feedback_sent"))


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Plain text: either the awaited feedback message, or a nudge towards /help."""
    deps = get_deps(context)
    lang = user_language(update, deps)
    if context.user_data.pop("awaiting_feedback", False):
        await _forward_feedback(update, context, update.effective_message.text, lang)
    else:
        await update.effective_message.reply_text(deps.i18n.t(lang, "help"))


async def other_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stickers, photos etc. from students."""
    deps = get_deps(context)
    lang = user_language(update, deps)
    if context.user_data.get("awaiting_feedback"):
        await update.effective_message.reply_text(deps.i18n.t(lang, "feedback_text_only"))
    else:
        await update.effective_message.reply_text(deps.i18n.t(lang, "help"))


# --- admin commands -----------------------------------------------------------


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = get_deps(context)
    if not is_admin(update, deps):
        await help_command(update, context)
        return
    today = today_local()
    s = deps.db.stats(today)
    requests = deps.counter.last_7_days(today)
    status = "⏸ paused (/resume)" if deps.db.is_paused() else "▶️ on (/pause)"
    text = (
        "📊 Stats\n\n"
        f"Daily messages: {status}\n\n"
        f"Users: {s.total}\n"
        f"Active subscribers: {s.subscribed}\n"
        f"Stopped: {s.unsubscribed}\n"
        f"Türkçe / English: {s.turkish} / {s.english}\n\n"
        f"New today: {s.new_today}\n"
        f"New this week: {s.new_this_week}\n"
        f"New this month: {s.new_this_month}\n\n"
        "Requests, last 7 days (since last restart):\n"
        f"/menu: {requests.get('menu', 0)}\n"
        f"/tomorrow: {requests.get('tomorrow', 0)}"
    )
    await update.effective_message.reply_text(text)


async def _send_preview(update: Update, pending: PendingMenu) -> None:
    preview = pipeline.format_preview(pending.result, pending.year, pending.month)
    for part in pipeline.split_message(preview):
        await update.effective_message.reply_text(part)


async def admin_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin sent the monthly menu picture (as a photo or an image file)."""
    deps = get_deps(context)
    if not is_admin(update, deps):
        await other_message(update, context)
        return
    message = update.effective_message

    if message.photo:
        tg_file = await message.photo[-1].get_file()  # largest size
        media_type, ext = "image/jpeg", ".jpg"
    else:
        doc = message.document
        tg_file = await doc.get_file()
        media_type = doc.mime_type or "image/jpeg"
        ext = {"image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}.get(media_type, ".jpg")

    if not deps.config.llm_api_key:
        await message.reply_text("LLM_API_KEY is not set, so photos can't be read. Upload a JSON file instead.")
        return

    year, month = pipeline.parse_month_hint(message.caption) or pipeline.default_target_month(today_local())
    image = bytes(await tg_file.download_as_bytearray())
    await message.reply_text(
        f"⏳ Reading the {year}-{month:02d} menu… "
        "(add a caption like 2026-10 to choose another month)"
    )

    try:
        data = await pipeline.extract_menu(
            image, media_type, year, month, deps.config.llm_api_key, deps.config.llm_model
        )
    except Exception as exc:  # API errors, parsing errors: all reported the same way
        logger.error("AI extraction failed: %s", exc)
        await message.reply_text(f"⚠️ AI extraction failed: {exc}\n\nResend the photo or upload a JSON file.")
        return

    result = pipeline.validate_menu(data, year, month)
    deps.pending = PendingMenu(year, month, result, image, ext)
    await _send_preview(update, deps.pending)


async def admin_json(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin uploaded a hand-fixed YYYY-MM.json file."""
    deps = get_deps(context)
    if not is_admin(update, deps):
        await other_message(update, context)
        return
    message = update.effective_message
    doc = message.document
    hint = pipeline.parse_month_hint(doc.file_name) or pipeline.parse_month_hint(message.caption)
    if hint is None:
        await message.reply_text("Name the file like 2026-10.json (or add the caption 2026-10).")
        return
    year, month = hint
    raw = bytes(await (await doc.get_file()).download_as_bytearray())
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        await message.reply_text(f"❌ That file is not valid JSON: {exc}")
        return
    result = pipeline.validate_menu(data, year, month)
    # Keep the existing picture (if any) as the source of truth
    deps.pending = PendingMenu(year, month, result, image=None)
    await _send_preview(update, deps.pending)


async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = get_deps(context)
    if not is_admin(update, deps):
        await help_command(update, context)
        return
    pending = deps.pending
    if pending is None:
        await update.effective_message.reply_text("Nothing to approve. Send a menu photo first.")
        return
    if not pending.result.ok:
        await update.effective_message.reply_text(
            "❌ This menu has errors and can't be approved. /reject it and resend or fix the JSON."
        )
        return
    path = pipeline.save_menu(
        deps.config.menu_dir, pending.year, pending.month, pending.result.menu, pending.image, pending.image_ext
    )
    deps.store.clear_cache()
    deps.pending = None
    logger.info("Menu %s approved", path.name)
    await update.effective_message.reply_text(
        f"✅ Saved {path.name} ({len(pending.result.menu)} days). Students will see it now.\n"
        "Remember to commit it to Git so the month has a history."
    )
    if deps.db.is_paused():
        await update.effective_message.reply_text(
            "⏸ Daily messages are still paused. Send /resume to turn them back on."
        )


async def reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = get_deps(context)
    if not is_admin(update, deps):
        await help_command(update, context)
        return
    if deps.pending is None:
        await update.effective_message.reply_text("Nothing to reject.")
        return
    deps.pending = None
    await update.effective_message.reply_text(
        "🗑 Discarded. Resend the photo, or upload a fixed JSON file named like 2026-10.json."
    )


def _first_unpublished_month(deps: Deps, today: date) -> date:
    """This month if its file is missing, otherwise next month (pausing ahead of time)."""
    if not deps.store.path_for(today.year, today.month).exists():
        return today.replace(day=1)
    return (today.replace(day=28) + timedelta(days=4)).replace(day=1)


async def pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stop the daily message for everyone (e.g. GTU hasn't published the menu yet)."""
    deps = get_deps(context)
    if not is_admin(update, deps):
        await help_command(update, context)
        return
    was_paused = deps.db.is_paused()
    deps.db.set_paused(True)
    logger.info("Daily messages paused by admin")
    month = _first_unpublished_month(deps, today_local())
    name = month_names(deps, month.month)["en"]
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📭 Tell everyone: {name} menu not published", callback_data="admin:notify:not_published")],
        [InlineKeyboardButton("⏸ Tell everyone: short pause", callback_data="admin:notify:paused")],
        [InlineKeyboardButton("🔕 Don't tell anyone", callback_data="admin:notify:none")],
    ])
    await update.effective_message.reply_text(
        ("⏸ Already paused." if was_paused else "⏸ Daily messages paused for all subscribers.")
        + f"\n\nShould I send the {len(deps.db.subscribers())} subscribers a notice?",
        reply_markup=keyboard,
    )


async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = get_deps(context)
    if not is_admin(update, deps):
        await help_command(update, context)
        return
    if not deps.db.is_paused():
        await update.effective_message.reply_text("▶️ Daily messages are already on.")
        return
    deps.db.set_paused(False)
    logger.info("Daily messages resumed by admin")
    today = today_local()
    lines = ["▶️ Daily messages are back on."]
    if not deps.store.path_for(today.year, today.month).exists():
        lines.append(f"⚠️ There is still no menu file for {today:%Y-%m}. Send the photo first.")
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📣 Tell everyone", callback_data="admin:notify:resumed"),
        InlineKeyboardButton("🔕 Don't", callback_data="admin:notify:none"),
    ]])
    await update.effective_message.reply_text("\n".join(lines), reply_markup=keyboard)


async def notice_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin's answer to 'should I tell everyone?' after /pause or /resume."""
    deps = get_deps(context)
    query = update.callback_query
    await query.answer()
    if not is_admin(update, deps):
        return
    choice = (query.data or "").removeprefix("admin:notify:")
    original = query.message.text
    if choice == "none":
        await query.edit_message_text(original + "\n\n🔕 No notice sent.")
        return

    if choice == "not_published":
        month = _first_unpublished_month(deps, today_local())
        kwargs = {"month": month_names(deps, month.month)}
    elif choice == "resumed":
        kwargs = {"time": f"{deps.config.daily_hour:02d}:{deps.config.daily_minute:02d}"}
    elif choice == "paused":
        kwargs = {}
    else:
        return
    # Remove the buttons first so a double tap can't send the notice twice
    await query.edit_message_text(original + "\n\n📣 Sending notice…")

    async def send_and_report() -> None:
        result = await send_notice(context.bot, deps.db, deps.i18n, f"notice_{choice}", **kwargs)
        extra = f" ({result.failed} failed, {result.blocked} had blocked the bot)" if result.failed or result.blocked else ""
        await query.edit_message_text(original + f"\n\n📣 Notice sent to {result.sent} subscribers{extra}.")

    # Sending to every subscriber takes a while; run it in the background so the
    # bot keeps answering students meanwhile.
    context.application.create_task(send_and_report(), update=update)


# --- errors -------------------------------------------------------------------


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled error while handling an update", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            deps = get_deps(context)
            await update.effective_message.reply_text(deps.i18n.t(user_language(update, deps), "error"))
        except Exception:  # never let the error handler itself crash
            logger.exception("Could not send the error message")
