import json
from datetime import date

import pytest

from bot.pipeline import (
    PipelineError,
    default_target_month,
    format_preview,
    parse_month_hint,
    parse_response,
    save_menu,
    split_message,
    validate_menu,
)
from bot.menu import MenuStore


def full_month(year=2026, month=10):
    """A correct month: every weekday of October 2026 has two dishes."""
    import calendar

    data = {}
    for d in range(1, calendar.monthrange(year, month)[1] + 1):
        day = date(year, month, d)
        if day.weekday() < 5:
            data[day.isoformat()] = [
                {"tr": "Mercimek çorbası", "en": "Red lentil soup"},
                {"tr": "Pilav", "en": "Rice pilaf"},
            ]
    return data


# --- parsing the AI reply ------------------------------------------------------


def test_parse_plain_json():
    assert parse_response('{"2026-10-01": []}') == {"2026-10-01": []}


def test_parse_json_in_markdown_fence():
    reply = 'Here you go:\n```json\n{"2026-10-01": [{"tr": "Pilav", "en": "Rice"}]}\n```'
    assert parse_response(reply)["2026-10-01"][0]["tr"] == "Pilav"


def test_parse_json_with_surrounding_prose():
    assert parse_response('Sure! {"a": 1} Hope it helps.') == {"a": 1}


@pytest.mark.parametrize("reply", ["I can't read this image.", '{"2026-10-01": [', "[1, 2]"])
def test_parse_broken_reply_raises(reply):
    with pytest.raises(PipelineError):
        parse_response(reply)


# --- validation ----------------------------------------------------------------


def test_correct_month_passes_cleanly():
    result = validate_menu(full_month(), 2026, 10)
    assert result.ok
    assert result.warnings == []
    assert len(result.menu) == 22  # weekdays in October 2026


def test_dates_outside_month_and_invalid_dates_are_errors():
    data = full_month()
    data["2026-11-02"] = [{"tr": "Pilav", "en": "Rice"}]
    data["2026-10-32"] = [{"tr": "Pilav", "en": "Rice"}]
    result = validate_menu(data, 2026, 10)
    assert not result.ok
    assert any("outside 2026-10" in e for e in result.errors)
    assert any("2026-10-32" in e for e in result.errors)


def test_empty_day_and_empty_names_are_errors():
    data = full_month()
    data["2026-10-01"] = []
    data["2026-10-02"] = [{"tr": "Pilav", "en": ""}, {"tr": "", "en": "Soup"}]
    result = validate_menu(data, 2026, 10)
    assert not result.ok
    assert any("2026-10-01 has no dishes" in e for e in result.errors)
    assert any("empty 'en'" in e for e in result.errors)
    assert any("empty 'tr'" in e for e in result.errors)


def test_missing_weekdays_are_listed_as_warnings():
    data = full_month()
    del data["2026-10-29"]  # Republic Day
    result = validate_menu(data, 2026, 10)
    assert result.ok
    assert result.missing_weekdays == [date(2026, 10, 29)]
    assert any("Weekdays without a menu: 29" in w for w in result.warnings)


def test_too_many_dishes_is_a_warning():
    data = full_month()
    data["2026-10-01"] = [{"tr": f"Yemek {i} çorba", "en": f"Dish {i}"} for i in range(9)]
    result = validate_menu(data, 2026, 10)
    assert result.ok
    assert any("9 dishes" in w for w in result.warnings)


def test_missing_turkish_letters_are_flagged():
    data = {k: [{"tr": "Mercimek corbasi", "en": "Red lentil soup"}] for k in full_month()}
    result = validate_menu(data, 2026, 10)
    assert any("No Turkish letters" in w for w in result.warnings)
    assert any("missing Turkish letters" in w for w in result.warnings)


def test_plain_turkish_words_without_special_letters_are_not_flagged():
    data = full_month()
    data["2026-10-01"] = [{"tr": "Kuru fasulye", "en": "White bean stew"}, {"tr": "Pilav", "en": "Rice"}]
    result = validate_menu(data, 2026, 10)
    assert result.warnings == []


def test_whitespace_is_trimmed_and_days_sorted():
    data = {"2026-10-02": [{"tr": " Pilav ", "en": "Rice "}], "2026-10-01": [{"tr": "Çorba", "en": "Soup"}]}
    result = validate_menu(data, 2026, 10)
    assert list(result.menu) == ["2026-10-01", "2026-10-02"]
    assert result.menu["2026-10-02"] == [{"tr": "Pilav", "en": "Rice"}]


def test_not_an_object_is_an_error():
    assert not validate_menu(["nope"], 2026, 10).ok


# --- preview, saving, helpers --------------------------------------------------


def test_preview_puts_warnings_first():
    data = full_month()
    del data["2026-10-29"]
    result = validate_menu(data, 2026, 10)
    preview = format_preview(result, 2026, 10)
    assert preview.index("warning") < preview.index("Thu 01")
    assert "/approve" in preview


def test_preview_with_errors_does_not_offer_approve():
    result = validate_menu({"2026-10-01": []}, 2026, 10)
    preview = format_preview(result, 2026, 10)
    assert "cannot approve" in preview
    assert "Send /approve" not in preview


def test_split_message_respects_limit():
    text = "\n".join(f"line {i}" for i in range(2000))
    parts = split_message(text, limit=500)
    assert all(len(p) <= 500 for p in parts)
    assert "\n".join(parts) == text


def test_save_menu_writes_readable_json_and_picture(tmp_path):
    result = validate_menu(full_month(), 2026, 10)
    path = save_menu(tmp_path, 2026, 10, result.menu, b"\xff\xd8fake-jpeg")
    assert path.name == "2026-10.json"
    assert (tmp_path / "2026-10.jpg").read_bytes() == b"\xff\xd8fake-jpeg"
    text = path.read_text(encoding="utf-8")
    assert "Mercimek çorbası" in text  # real letters, not \u escapes
    assert MenuStore(tmp_path).get_day(date(2026, 10, 1))[0]["en"] == "Red lentil soup"
    assert json.loads(text) == result.menu


def test_month_hint_and_default_month():
    assert parse_month_hint("2026-10") == (2026, 10)
    assert parse_month_hint("menu 2026-11.json") == (2026, 11)
    assert parse_month_hint("october") is None
    assert parse_month_hint(None) is None
    assert default_target_month(date(2026, 10, 5)) == (2026, 10)
    assert default_target_month(date(2026, 10, 25)) == (2026, 11)
    assert default_target_month(date(2026, 12, 28)) == (2027, 1)


# --- extraction retry (AI call replaced by a fake) -------------------------------


def test_extract_retries_once_then_succeeds(monkeypatch):
    import asyncio
    from bot import pipeline

    replies = iter(["not json at all", '{"2026-10-01": [{"tr": "Pilav", "en": "Rice"}]}'])

    async def fake_llm(*args, **kwargs):
        return next(replies)

    monkeypatch.setattr(pipeline, "call_vision_llm", fake_llm)
    data = asyncio.run(pipeline.extract_menu(b"img", "image/jpeg", 2026, 10, "key", "model"))
    assert data["2026-10-01"][0]["tr"] == "Pilav"


def test_extract_gives_up_after_two_bad_replies(monkeypatch):
    import asyncio
    from bot import pipeline

    async def fake_llm(*args, **kwargs):
        return "sorry, I can't"

    monkeypatch.setattr(pipeline, "call_vision_llm", fake_llm)
    with pytest.raises(PipelineError, match="failed twice"):
        asyncio.run(pipeline.extract_menu(b"img", "image/jpeg", 2026, 10, "key", "model"))


# --- calories ----------------------------------------------------------------------


def test_calories_are_kept_and_cleaned():
    data = full_month()
    data["2026-10-01"] = [
        {"tr": "Mercimek çorbası", "en": "Lentil soup", "kcal": 209},
        {"tr": "Pirinç pilavı", "en": "Rice pilaf", "kcal": "416CAL"},
        {"tr": "Meyve", "en": "Fruit", "kcal": None},
    ]
    result = validate_menu(data, 2026, 10)
    assert result.ok and result.warnings == []
    assert result.menu["2026-10-01"] == [
        {"tr": "Mercimek çorbası", "en": "Lentil soup", "kcal": 209},
        {"tr": "Pirinç pilavı", "en": "Rice pilaf", "kcal": 416},
        {"tr": "Meyve", "en": "Fruit"},
    ]


def test_suspicious_or_unreadable_calories_are_warnings():
    data = full_month()
    data["2026-10-01"] = [
        {"tr": "Yeşil salata", "en": "Green salad", "kcal": 5},
        {"tr": "Pilav", "en": "Rice", "kcal": "a lot"},
    ]
    result = validate_menu(data, 2026, 10)
    assert result.ok
    assert any("5 kcal" in w for w in result.warnings)
    assert any("unreadable calorie" in w for w in result.warnings)
    assert "kcal" not in result.menu["2026-10-01"][1]


def test_prompt_asks_for_calories():
    from bot.pipeline import build_prompt

    prompt = build_prompt(2026, 10)
    assert '"kcal"' in prompt and "October 2026" in prompt


# --- choices (rice OR pasta) --------------------------------------------------------


def test_choices_are_validated_and_kept():
    data = full_month()
    data["2026-10-01"] = [
        {"options": [{"tr": " Pirinç pilavı ", "en": "Rice pilaf", "kcal": "416CAL"},
                     {"tr": "Makarna", "en": "Pasta", "kcal": 331}]},
    ]
    result = validate_menu(data, 2026, 10)
    assert result.ok and result.warnings == []
    assert result.menu["2026-10-01"] == [{"options": [
        {"tr": "Pirinç pilavı", "en": "Rice pilaf", "kcal": 416},
        {"tr": "Makarna", "en": "Pasta", "kcal": 331},
    ]}]
    preview = format_preview(result, 2026, 10)
    assert "Pirinç pilavı OR Makarna  |  Rice pilaf OR Pasta — 416 / 331 kcal" in preview


def test_choice_with_one_usable_dish_becomes_a_normal_dish():
    data = full_month()
    data["2026-10-01"] = [{"options": [{"tr": "Pirinç pilavı", "en": "Rice"}, {"tr": "", "en": "Pasta"}]}]
    result = validate_menu(data, 2026, 10)
    assert result.menu["2026-10-01"] == [{"tr": "Pirinç pilavı", "en": "Rice"}]
    assert any("only one dish" in w for w in result.warnings)


def test_prompt_mentions_choices_and_leaves_out_vegan_dishes():
    from bot import pipeline

    prompt = pipeline.build_prompt(2026, 11)
    assert '{"options": [' in prompt
    assert "vegan" in prompt
