from datetime import date

import pytest

from bot.db import RequestCounter

TODAY = date(2026, 10, 14)  # a Wednesday


def test_new_user_is_subscribed(db):
    user, is_new = db.start_user(1, "tr", TODAY)
    assert is_new
    assert user.subscribed and user.language == "tr" and user.joined_at == TODAY
    assert db.subscribers() == [(1, "tr")]


def test_stop_and_resubscribe(db):
    db.start_user(1, "en", TODAY)
    db.set_subscribed(1, False)
    assert db.subscribers() == []

    user, is_new = db.start_user(1, "tr", date(2026, 10, 20))
    assert not is_new
    assert user.subscribed
    assert user.language == "en"             # returning users keep their language
    assert user.joined_at == TODAY           # and their original join date


def test_language_change(db):
    db.start_user(1, "tr", TODAY)
    db.set_language(1, "en")
    assert db.get_user(1).language == "en"
    with pytest.raises(ValueError):
        db.set_language(1, "ar")


def test_only_four_fields_are_stored(db):
    columns = [row[1] for row in db.conn.execute("PRAGMA table_info(users)")]
    assert columns == ["chat_id", "language", "subscribed", "joined_at"]


def test_stats_counts(db):
    db.start_user(1, "tr", TODAY)                   # today
    db.start_user(2, "en", date(2026, 10, 12))      # Monday this week
    db.start_user(3, "tr", date(2026, 10, 2))       # earlier this month
    db.start_user(4, "en", date(2026, 9, 20))       # last month
    db.set_subscribed(4, False)

    s = db.stats(TODAY)
    assert (s.total, s.subscribed, s.unsubscribed) == (4, 3, 1)
    assert (s.turkish, s.english) == (2, 2)
    assert (s.new_today, s.new_this_week, s.new_this_month) == (1, 2, 3)


def test_request_counter_last_7_days():
    c = RequestCounter()
    c.add("menu", date(2026, 10, 1))   # too old
    c.add("menu", date(2026, 10, 8))   # first day of the 7-day window
    c.add("menu", TODAY)
    c.add("tomorrow", TODAY)
    assert c.last_7_days(TODAY) == {"menu": 2, "tomorrow": 1}


def test_pause_setting_survives_restart(tmp_path):
    from bot.db import UserDB

    db = UserDB(tmp_path / "users.db")
    assert not db.is_paused()
    db.set_paused(True)
    db.close()
    reopened = UserDB(tmp_path / "users.db")
    assert reopened.is_paused()
    reopened.set_paused(False)
    assert not reopened.is_paused()
    reopened.close()
