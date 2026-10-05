"""Loads monthly menu JSON files, finds a day's menu and formats it in TR or EN.

File layout: data/menus/YYYY-MM.json, e.g.
    {"2026-10-14": [{"tr": "Mercimek çorbası", "en": "Red lentil soup", "kcal": 209}, ...], ...}

"kcal" is optional: dishes like "Meyve" (fruit) often have no calorie value.

A line where students pick ONE of several dishes (e.g. rice or pasta) is an options group:
    {"options": [{"tr": "Pirinç pilavı", "en": "Rice pilaf", "kcal": 416},
                 {"tr": "Makarna", "en": "Pasta", "kcal": 331}]}
and is shown as "Pirinç pilavı / Makarna — 416 / 331 kcal".

A missing date means there is no menu that day. A missing or broken file never
crashes the bot: lookups return None and the caller decides whether to alert.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import TIMEZONE
from .i18n import I18n

logger = logging.getLogger(__name__)

Dish = dict          # {"tr": ..., "en": ..., "kcal": ...}
MenuItem = dict      # a Dish, or {"options": [Dish, Dish, ...]}
MonthMenu = dict[str, list[MenuItem]]


def item_choices(item: MenuItem) -> list[Dish]:
    """The dishes a menu line offers: several for an options group, otherwise just one."""
    return item["options"] if isinstance(item.get("options"), list) else [item]


class MenuUnavailable(Exception):
    """The month's file is missing or cannot be used."""

    def __init__(self, year: int, month: int, reason: str, missing: bool = False):
        self.year, self.month, self.reason = year, month, reason
        # missing=True: the file doesn't exist yet, i.e. GTU hasn't published that month.
        # missing=False: the file exists but is broken (an admin problem, not a student one).
        self.missing = missing
        super().__init__(f"Menu {year}-{month:02d} unavailable: {reason}")


def month_key(year: int, month: int) -> str:
    return f"{year}-{month:02d}"


def check_menu_structure(data: object) -> list[str]:
    """Return a list of structural problems (empty list means the shape is fine)."""
    problems: list[str] = []
    if not isinstance(data, dict):
        return ["top level is not an object"]
    for day, dishes in data.items():
        try:
            date.fromisoformat(day)
        except (TypeError, ValueError):
            problems.append(f"{day!r} is not a YYYY-MM-DD date")
            continue
        if not isinstance(dishes, list):
            problems.append(f"{day}: dishes are not a list")
            continue
        for i, item in enumerate(dishes, 1):
            if not isinstance(item, dict):
                problems.append(f"{day} dish {i}: not an object")
                continue
            if "options" in item:
                options = item["options"]
                if not isinstance(options, list) or len(options) < 2:
                    problems.append(f"{day} dish {i}: 'options' needs at least two dishes")
                    continue
                for j, option in enumerate(options, 1):
                    problems += _dish_problems(option, f"{day} dish {i} option {j}")
            else:
                problems += _dish_problems(item, f"{day} dish {i}")
    return problems


def _dish_problems(dish: object, where: str) -> list[str]:
    if not isinstance(dish, dict):
        return [f"{where}: not an object"]
    problems = []
    for lang in ("tr", "en"):
        value = dish.get(lang)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{where}: missing '{lang}' name")
    if "kcal" in dish and dish["kcal"] is not None and not is_valid_kcal(dish["kcal"]):
        problems.append(f"{where}: 'kcal' must be a positive whole number")
    return problems


def is_valid_kcal(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


class MenuStore:
    """Reads data/menus/*.json on request and caches each month in memory."""

    def __init__(self, menu_dir: Path):
        self.menu_dir = Path(menu_dir)
        self._cache: dict[str, MonthMenu] = {}

    def path_for(self, year: int, month: int, suffix: str = ".json") -> Path:
        return self.menu_dir / f"{month_key(year, month)}{suffix}"

    def load_month(self, year: int, month: int) -> MonthMenu:
        """Return the month's menu or raise MenuUnavailable."""
        key = month_key(year, month)
        if key in self._cache:
            return self._cache[key]

        path = self.path_for(year, month)
        if not path.exists():
            raise MenuUnavailable(year, month, "file not found", missing=True)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("Could not read menu file %s: %s", path, exc)
            raise MenuUnavailable(year, month, f"file is not valid JSON ({exc})") from exc

        problems = check_menu_structure(data)
        if problems:
            logger.error("Menu file %s has problems: %s", path, problems[:5])
            raise MenuUnavailable(year, month, f"invalid content: {problems[0]}")

        self._cache[key] = data
        return data

    def get_day(self, day: date) -> list[Dish] | None:
        """Dishes for a day, or None when there is no menu (or the file is unusable)."""
        try:
            data = self.load_month(day.year, day.month)
        except MenuUnavailable as exc:
            logger.warning("%s", exc)
            return None
        dishes = data.get(day.isoformat())
        return dishes or None

    def clear_cache(self) -> None:
        self._cache.clear()


def today_local(now: datetime | None = None) -> date:
    """Today's date in Europe/Istanbul, whatever the server's timezone is."""
    now = now or datetime.now(TIMEZONE)
    return now.astimezone(TIMEZONE).date()


def tomorrow_local(now: datetime | None = None) -> date:
    return today_local(now) + timedelta(days=1)


def format_date(day: date, lang: str, i18n: I18n) -> str:
    months = i18n.get(lang, "months")
    weekdays = i18n.get(lang, "weekdays")
    return i18n.t(
        lang,
        "date_format",
        day=day.day,
        month=months[day.month - 1],
        year=day.year,
        weekday=weekdays[day.weekday()],
    )


def format_menu(day: date, dishes: list[MenuItem], lang: str, i18n: I18n) -> str:
    """Date header, one line per dish (or "A / B" choice) with calories, then the day's total.

    With a choice on the menu, the total is a range: lightest picks – heaviest picks.
    """
    lines = [format_date(day, lang, i18n), ""]
    low = high = 0
    for item in dishes:
        choices = item_choices(item)
        names = " / ".join(c.get(lang) or c.get("tr", "") for c in choices)
        kcals = [c.get("kcal") if is_valid_kcal(c.get("kcal")) else None for c in choices]
        if any(kcals):
            lines.append(f"• {names} — " + " / ".join(str(k) if k else "?" for k in kcals) + " kcal")
            low += min(k or 0 for k in kcals)
            high += max(k or 0 for k in kcals)
        else:
            lines.append(f"• {names}")
    if high:
        total = str(high) if low == high else f"{low}–{high}"
        lines += ["", i18n.t(lang, "kcal_total", kcal=total)]
    return "\n".join(lines)
