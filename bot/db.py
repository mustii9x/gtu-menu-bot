"""SQLite users table and in-memory usage counters.

Stored per user: chat_id, language, subscribed, joined_at — nothing else.
A separate `settings` table holds bot-wide switches (e.g. paused), no user data.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    chat_id    INTEGER PRIMARY KEY,
    language   TEXT    NOT NULL CHECK (language IN ('tr', 'en')),
    subscribed INTEGER NOT NULL DEFAULT 1,
    joined_at  TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class User:
    chat_id: int
    language: str
    subscribed: bool
    joined_at: date


@dataclass(frozen=True)
class Stats:
    total: int
    subscribed: int
    unsubscribed: int
    turkish: int
    english: int
    new_today: int
    new_this_week: int
    new_this_month: int


class UserDB:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # --- users ---------------------------------------------------------------

    def get_user(self, chat_id: int) -> User | None:
        row = self.conn.execute(
            "SELECT chat_id, language, subscribed, joined_at FROM users WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        if row is None:
            return None
        return User(row[0], row[1], bool(row[2]), date.fromisoformat(row[3]))

    def start_user(self, chat_id: int, language: str, today: date) -> tuple[User, bool]:
        """Subscribe a user. New users are created; returning users are re-subscribed
        and keep their stored language. Returns (user, is_new)."""
        existing = self.get_user(chat_id)
        if existing is None:
            self.conn.execute(
                "INSERT INTO users (chat_id, language, subscribed, joined_at) VALUES (?, ?, 1, ?)",
                (chat_id, language, today.isoformat()),
            )
            self.conn.commit()
            return User(chat_id, language, True, today), True
        if not existing.subscribed:
            self.set_subscribed(chat_id, True)
        return self.get_user(chat_id), False  # type: ignore[return-value]

    def set_language(self, chat_id: int, language: str) -> None:
        if language not in ("tr", "en"):
            raise ValueError(f"Unsupported language {language!r}")
        self.conn.execute("UPDATE users SET language = ? WHERE chat_id = ?", (language, chat_id))
        self.conn.commit()

    def set_subscribed(self, chat_id: int, subscribed: bool) -> None:
        self.conn.execute(
            "UPDATE users SET subscribed = ? WHERE chat_id = ?", (int(subscribed), chat_id)
        )
        self.conn.commit()

    def subscribers(self) -> list[tuple[int, str]]:
        """(chat_id, language) for everyone who should get the daily menu."""
        return list(
            self.conn.execute("SELECT chat_id, language FROM users WHERE subscribed = 1 ORDER BY chat_id")
        )

    # --- bot-wide settings ---------------------------------------------------

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    def is_paused(self) -> bool:
        """True while the admin has paused the daily message for everyone (survives restarts)."""
        return self.get_setting("paused", "0") == "1"

    def set_paused(self, paused: bool) -> None:
        self.set_setting("paused", "1" if paused else "0")

    # --- stats ---------------------------------------------------------------

    def stats(self, today: date) -> Stats:
        week_start = today - timedelta(days=today.weekday())
        month_start = today.replace(day=1)

        def count(where: str = "1=1", *params: object) -> int:
            return self.conn.execute(f"SELECT COUNT(*) FROM users WHERE {where}", params).fetchone()[0]

        return Stats(
            total=count(),
            subscribed=count("subscribed = 1"),
            unsubscribed=count("subscribed = 0"),
            turkish=count("language = 'tr'"),
            english=count("language = 'en'"),
            new_today=count("joined_at = ?", today.isoformat()),
            new_this_week=count("joined_at >= ?", week_start.isoformat()),
            new_this_month=count("joined_at >= ?", month_start.isoformat()),
        )


class RequestCounter:
    """Daily totals of /menu and /tomorrow requests, kept in memory (not per user)."""

    def __init__(self) -> None:
        self._counts: dict[date, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    def add(self, command: str, day: date) -> None:
        self._counts[day][command] += 1
        # keep memory bounded: drop anything older than 30 days
        for old in [d for d in self._counts if d < day - timedelta(days=30)]:
            del self._counts[old]

    def last_7_days(self, today: date) -> dict[str, int]:
        start = today - timedelta(days=6)
        totals: dict[str, int] = defaultdict(int)
        for d, commands in self._counts.items():
            if start <= d <= today:
                for command, n in commands.items():
                    totals[command] += n
        return dict(totals)
