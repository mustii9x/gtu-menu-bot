"""Looks up interface text in the user's language.

All user-facing text lives in locales/<lang>.json. If a key is missing in one
language, Turkish is used as the fallback and a warning is logged.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SUPPORTED_LANGUAGES = ("tr", "en")
FALLBACK_LANGUAGE = "tr"


def detect_language(telegram_language_code: str | None) -> str:
    """Turkish app language -> 'tr'; anything else (or unknown) -> 'en'."""
    if telegram_language_code and telegram_language_code.lower().startswith("tr"):
        return "tr"
    return "en"


class I18n:
    def __init__(self, locales_dir: Path):
        self.texts: dict[str, dict[str, Any]] = {}
        for lang in SUPPORTED_LANGUAGES:
            with open(Path(locales_dir) / f"{lang}.json", encoding="utf-8") as f:
                self.texts[lang] = json.load(f)

    def get(self, lang: str, key: str) -> Any:
        """Return the raw value (string or list) for a key."""
        if lang not in self.texts:
            lang = FALLBACK_LANGUAGE
        if key in self.texts[lang]:
            return self.texts[lang][key]
        if key in self.texts[FALLBACK_LANGUAGE]:
            logger.warning("Locale key %r missing for %r, falling back to Turkish", key, lang)
            return self.texts[FALLBACK_LANGUAGE][key]
        logger.error("Locale key %r missing in every language", key)
        return key

    def t(self, lang: str, key: str, **kwargs: Any) -> str:
        """Return a text, with {placeholders} filled in from kwargs."""
        value = self.get(lang, key)
        text = str(value)
        return text.format(**kwargs) if kwargs else text
