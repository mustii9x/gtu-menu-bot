"""Reads settings from the environment (.env file via python-dotenv).

Secrets never live in code: BOT_TOKEN, ADMIN_CHAT_ID and LLM_API_KEY come from .env,
which is listed in .gitignore.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TIMEZONE = ZoneInfo("Europe/Istanbul")


class ConfigError(RuntimeError):
    """Raised when a required setting is missing."""


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_chat_id: int
    llm_api_key: str
    llm_model: str
    menu_dir: Path
    locales_dir: Path
    db_path: Path
    daily_hour: int
    daily_minute: int


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required setting {name} (see .env.example)")
    return value


def load_config(env_file: Path | None = None) -> Config:
    load_dotenv(env_file or PROJECT_ROOT / ".env")

    admin_raw = _require("ADMIN_CHAT_ID")
    try:
        admin_chat_id = int(admin_raw)
    except ValueError as exc:
        raise ConfigError("ADMIN_CHAT_ID must be a number") from exc

    send_time = os.getenv("DAILY_SEND_TIME", "07:30")
    try:
        hour_s, minute_s = send_time.split(":")
        hour, minute = int(hour_s), int(minute_s)
    except ValueError as exc:
        raise ConfigError("DAILY_SEND_TIME must look like 07:30") from exc

    def path_setting(name: str, default: str) -> Path:
        p = Path(os.getenv(name, default))
        return p if p.is_absolute() else PROJECT_ROOT / p

    return Config(
        bot_token=_require("BOT_TOKEN"),
        admin_chat_id=admin_chat_id,
        # The AI key is optional at startup: without it, the bot still serves menus,
        # only the photo pipeline is disabled (manual JSON upload still works).
        llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
        llm_model=os.getenv("LLM_MODEL", "claude-sonnet-4-5").strip(),
        menu_dir=path_setting("MENU_DIR", "data/menus"),
        locales_dir=path_setting("LOCALES_DIR", "locales"),
        db_path=path_setting("DB_PATH", "data/users.db"),
        daily_hour=hour,
        daily_minute=minute,
    )
