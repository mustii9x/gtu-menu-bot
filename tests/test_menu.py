from datetime import date, datetime, timezone

import pytest

from bot.menu import MenuUnavailable, format_date, format_menu, today_local, tomorrow_local


def test_finds_a_days_menu(store):
    dishes = store.get_day(date(2026, 10, 14))
    assert [d["tr"] for d in dishes] == ["Mercimek çorbası", "Tavuk sote", "Pilav", "Ayran"]


def test_missing_day_means_no_menu(store):
    assert store.get_day(date(2026, 10, 17)) is None  # a Saturday, not in the file


def test_missing_month_file_returns_none_and_raises_on_load(store):
    assert store.get_day(date(2026, 12, 1)) is None
    with pytest.raises(MenuUnavailable, match="not found"):
        store.load_month(2026, 12)


def test_broken_month_file_never_crashes(store, menu_dir):
    (menu_dir / "2026-09.json").write_text("{ not json", encoding="utf-8")
    assert store.get_day(date(2026, 9, 1)) is None
    with pytest.raises(MenuUnavailable, match="not valid JSON"):
        store.load_month(2026, 9)


def test_file_with_wrong_shape_is_rejected(store, menu_dir):
    (menu_dir / "2026-08.json").write_text('{"2026-08-03": [{"tr": "Pilav"}]}', encoding="utf-8")
    with pytest.raises(MenuUnavailable, match="missing 'en'"):
        store.load_month(2026, 8)


def test_tomorrow_across_month_boundary(store, menu_dir):
    # Friday 30 Oct -> tomorrow is 31 Oct; Saturday 31 Oct -> tomorrow is in November
    now = datetime(2026, 10, 31, 9, 0, tzinfo=timezone.utc)
    assert tomorrow_local(now) == date(2026, 11, 1)
    (menu_dir / "2026-11.json").write_text(
        '{"2026-11-01": [{"tr": "Karnıyarık", "en": "Karnıyarık (stuffed eggplant)"}]}', encoding="utf-8"
    )
    assert store.get_day(tomorrow_local(now))[0]["tr"] == "Karnıyarık"


def test_istanbul_midnight_rolls_the_date(store):
    # 21:30 UTC on 1 Nov is already 00:30 on 2 Nov in Istanbul (UTC+3)
    now = datetime(2026, 11, 1, 21, 30, tzinfo=timezone.utc)
    assert today_local(now) == date(2026, 11, 2)
    assert store.get_day(today_local(now))[0]["tr"] == "Karnıyarık"


def test_today_uses_istanbul_time_not_server_time():
    late_utc = datetime(2026, 10, 13, 22, 30, tzinfo=timezone.utc)  # 01:30 on the 14th in Istanbul
    assert today_local(late_utc) == date(2026, 10, 14)


def test_cache_is_used_until_cleared(store, menu_dir):
    store.load_month(2026, 10)
    (menu_dir / "2026-10.json").write_text('{"2026-10-14": [{"tr": "Yeni", "en": "New"}]}', encoding="utf-8")
    assert store.get_day(date(2026, 10, 14))[0]["tr"] == "Mercimek çorbası"
    store.clear_cache()
    assert store.get_day(date(2026, 10, 14))[0]["tr"] == "Yeni"


def test_format_date_in_both_languages(i18n):
    d = date(2026, 10, 14)
    assert format_date(d, "tr", i18n) == "14 Ekim 2026, Çarşamba"
    assert format_date(d, "en", i18n) == "Wednesday, 14 October 2026"


def test_format_menu_turkish(store, i18n):
    d = date(2026, 10, 14)
    text = format_menu(d, store.get_day(d), "tr", i18n)
    assert text == "14 Ekim 2026, Çarşamba\n\n• Mercimek çorbası\n• Tavuk sote\n• Pilav\n• Ayran"


def test_format_menu_english(store, i18n):
    d = date(2026, 10, 14)
    text = format_menu(d, store.get_day(d), "en", i18n)
    assert text.splitlines()[0] == "Wednesday, 14 October 2026"
    assert "• Red lentil soup" in text
    assert "Mercimek" not in text


# --- calories ---------------------------------------------------------------------


def test_format_menu_with_calories_and_total(i18n):
    d = date(2026, 10, 5)
    dishes = [
        {"tr": "Mercimek çorbası", "en": "Lentil soup", "kcal": 209},
        {"tr": "Pirinç pilavı", "en": "Rice pilaf", "kcal": 416},
        {"tr": "İçecek", "en": "Drink"},  # no calorie value
    ]
    assert format_menu(d, dishes, "tr", i18n) == (
        "5 Ekim 2026, Pazartesi\n\n"
        "• Mercimek çorbası — 209 kcal\n"
        "• Pirinç pilavı — 416 kcal\n"
        "• İçecek\n\n"
        "🔥 Toplam: 625 kcal"
    )
    assert format_menu(d, dishes, "en", i18n).endswith("• Drink\n\n🔥 Total: 625 kcal")


def test_no_total_line_without_any_calories(store, i18n):
    d = date(2026, 10, 14)
    assert "kcal" not in format_menu(d, store.get_day(d), "en", i18n)


def test_bad_kcal_in_file_is_rejected(store, menu_dir):
    (menu_dir / "2026-07.json").write_text(
        '{"2026-07-01": [{"tr": "Pilav", "en": "Rice", "kcal": "lots"}]}', encoding="utf-8"
    )
    with pytest.raises(MenuUnavailable, match="kcal"):
        store.load_month(2026, 7)


# --- choices (rice OR pasta) --------------------------------------------------------


CHOICE_DAY = [
    {"tr": "Mercimek çorbası", "en": "Lentil soup", "kcal": 209},
    {"options": [
        {"tr": "Pirinç pilavı", "en": "Rice pilaf", "kcal": 416},
        {"tr": "Acı soslu makarna", "en": "Pasta with spicy sauce", "kcal": 322},
    ]},
    {"tr": "Meyve", "en": "Fruit"},
]


def test_choice_is_one_line_with_a_slash_and_total_is_a_range(i18n):
    d = date(2026, 10, 5)
    assert format_menu(d, CHOICE_DAY, "tr", i18n) == (
        "5 Ekim 2026, Pazartesi\n\n"
        "• Mercimek çorbası — 209 kcal\n"
        "• Pirinç pilavı / Acı soslu makarna — 416 / 322 kcal\n"
        "• Meyve\n\n"
        "🔥 Toplam: 531–625 kcal"
    )
    assert "• Rice pilaf / Pasta with spicy sauce — 416 / 322 kcal" in format_menu(d, CHOICE_DAY, "en", i18n)


def test_choice_with_a_missing_calorie_value(i18n):
    day = [{"options": [{"tr": "Pilav", "en": "Rice", "kcal": 400}, {"tr": "Börek", "en": "Börek"}]}]
    text = format_menu(date(2026, 10, 5), day, "en", i18n)
    assert "• Rice / Börek — 400 / ? kcal" in text
    assert text.endswith("🔥 Total: 0–400 kcal")


def test_menu_file_with_choices_loads(store, menu_dir):
    import json as _json

    (menu_dir / "2026-06.json").write_text(_json.dumps({"2026-06-01": CHOICE_DAY}), encoding="utf-8")
    assert store.get_day(date(2026, 6, 1))[1]["options"][1]["tr"] == "Acı soslu makarna"


@pytest.mark.parametrize("bad", [
    {"options": [{"tr": "Pilav", "en": "Rice"}]},                       # only one choice
    {"options": [{"tr": "Pilav", "en": "Rice"}, {"tr": "Makarna"}]},    # missing 'en'
])
def test_broken_choices_are_rejected(store, menu_dir, bad):
    import json as _json

    (menu_dir / "2026-05.json").write_text(_json.dumps({"2026-05-04": [bad]}), encoding="utf-8")
    with pytest.raises(MenuUnavailable):
        store.load_month(2026, 5)


def test_real_october_menu_is_valid():
    """The committed data/menus/2026-10.json must always load and validate."""
    from bot.menu import MenuStore
    from bot.pipeline import validate_menu
    from tests.conftest import ROOT

    store = MenuStore(ROOT / "data" / "menus")
    data = store.load_month(2026, 10)
    assert validate_menu(data, 2026, 10).ok
    assert not any(
        d["tr"].startswith("Zeytinyağlı")
        for items in data.values() for it in items for d in it.get("options", [it])
    )
