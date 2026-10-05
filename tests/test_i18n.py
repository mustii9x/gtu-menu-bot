import json
import logging

from bot.i18n import I18n, detect_language
from tests.conftest import ROOT


def test_both_locales_have_exactly_the_same_keys():
    tr = json.loads((ROOT / "locales" / "tr.json").read_text(encoding="utf-8"))
    en = json.loads((ROOT / "locales" / "en.json").read_text(encoding="utf-8"))
    assert set(tr) == set(en), f"only in tr: {set(tr) - set(en)}, only in en: {set(en) - set(tr)}"


def test_month_and_weekday_lists_are_complete(i18n):
    for lang in ("tr", "en"):
        assert len(i18n.get(lang, "months")) == 12
        assert len(i18n.get(lang, "weekdays")) == 7


def test_lookup_in_users_language(i18n):
    assert i18n.t("tr", "no_menu_today") == "Bugün menü yok."
    assert i18n.t("en", "no_menu_today") == "No menu today."


def test_placeholders(i18n):
    assert "07:30" in i18n.t("en", "welcome", time="07:30")


def test_missing_key_falls_back_to_turkish(tmp_path, caplog):
    (tmp_path / "tr.json").write_text('{"only_tr": "Sadece Türkçe"}', encoding="utf-8")
    (tmp_path / "en.json").write_text("{}", encoding="utf-8")
    i18n = I18n(tmp_path)
    with caplog.at_level(logging.WARNING):
        assert i18n.t("en", "only_tr") == "Sadece Türkçe"
    assert "falling back to Turkish" in caplog.text


def test_detect_language():
    assert detect_language("tr") == "tr"
    assert detect_language("tr-TR") == "tr"
    assert detect_language("en") == "en"
    assert detect_language("ar") == "en"
    assert detect_language(None) == "en"
