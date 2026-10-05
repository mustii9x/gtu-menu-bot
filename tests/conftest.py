import json
from pathlib import Path

import pytest

from bot.db import UserDB
from bot.i18n import I18n
from bot.menu import MenuStore

ROOT = Path(__file__).resolve().parent.parent

OCTOBER = {
    "2026-10-14": [
        {"tr": "Mercimek çorbası", "en": "Red lentil soup"},
        {"tr": "Tavuk sote", "en": "Chicken sauté"},
        {"tr": "Pilav", "en": "Rice pilaf"},
        {"tr": "Ayran", "en": "Ayran (yogurt drink)"},
    ],
    "2026-10-30": [{"tr": "Ezogelin çorbası", "en": "Ezogelin soup"}],
}
NOVEMBER = {
    "2026-11-02": [{"tr": "Karnıyarık", "en": "Karnıyarık (stuffed eggplant with minced meat)"}],
}


@pytest.fixture
def i18n():
    return I18n(ROOT / "locales")


@pytest.fixture
def menu_dir(tmp_path):
    d = tmp_path / "menus"
    d.mkdir()
    (d / "2026-10.json").write_text(json.dumps(OCTOBER, ensure_ascii=False), encoding="utf-8")
    (d / "2026-11.json").write_text(json.dumps(NOVEMBER, ensure_ascii=False), encoding="utf-8")
    return d


@pytest.fixture
def store(menu_dir):
    return MenuStore(menu_dir)


@pytest.fixture
def db(tmp_path):
    database = UserDB(tmp_path / "users.db")
    yield database
    database.close()
