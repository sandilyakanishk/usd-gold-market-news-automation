"""Daily clean-up: the previous day's "this was sent" records are deleted each morning."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src.database.database import SQLiteRepository
from src.delivery.models import DeliveryRecord
from src.pipeline import TWO_DAY_TYPES, cleanup_old_deliveries

IST = ZoneInfo("Asia/Kolkata")
SETTINGS = SimpleNamespace(calendar_retention_days=14, display_timezone="Asia/Kolkata")


def ist(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=IST)


def record(key, message_type, moment, status="SENT"):
    stamp = moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sent = status == "SENT"
    return DeliveryRecord(message_key=key, provider="telegram", destination_id="@example_channel",
                          destination="telegram_channel", message_type=message_type, status=status,
                          provider_message_id="1" if sent else None, sent_at=stamp if sent else None,
                          error=None if sent else "boom", created_at=stamp, updated_at=stamp)


@pytest.fixture
def db():
    with SQLiteRepository(":memory:") as repo:
        yield repo


def keys(db):
    return sorted(r.message_key for r in db.list_deliveries(limit=500))


def fill(db):
    for item in [
        record("pulse_yesterday", "MARKET_PULSE", ist(8, 21)), record("corner_yesterday", "TRADER_CORNER", ist(8, 22)),
        record("brief_yesterday", "MORNING_UPDATE", ist(8, 8, 20)), record("recap_yesterday", "DAILY_RECAP", ist(8, 23, 35)),
        record("reel_yesterday", "VIDEO_POST", ist(8, 23)), record("result_yesterday", "ACTUAL_RESULT", ist(8, 18, 10)),
        record("live_yesterday", "LIVE_ALERT", ist(8, 19)), record("alert_yesterday", "HIGH_ALERT", ist(8, 9)),
        record("reel_two_days_ago", "VIDEO_POST", ist(7, 23)), record("result_two_days_ago", "ACTUAL_RESULT", ist(7, 18)),
        record("failed_yesterday", "MARKET_PULSE", ist(8, 13), status="FAILED"),
        record("brief_today", "MORNING_UPDATE", ist(9, 8, 16)), record("pulse_today", "MARKET_PULSE", ist(9, 9)),
    ]:
        db.save_delivery(item)


def test_nothing_is_deleted_before_the_mornings_first_message(db):
    fill(db)
    before = keys(db)
    for moment in (ist(9, 0, 5), ist(9, 6), ist(9, 8, 14)):
        assert cleanup_old_deliveries(SETTINGS, db, moment) == 0
    assert keys(db) == before


def test_from_the_morning_on_yesterdays_records_go_and_todays_stay(db):
    fill(db)
    assert cleanup_old_deliveries(SETTINGS, db, ist(9, 8, 16)) == 7
    assert keys(db) == ["alert_yesterday", "brief_today", "live_yesterday", "pulse_today", "reel_yesterday", "result_yesterday"]
    # Later runs the same day find nothing more to delete, and never touch today's records.
    for moment in (ist(9, 12), ist(9, 23, 59)):
        assert cleanup_old_deliveries(SETTINGS, db, moment) == 0
    assert "pulse_today" in keys(db) and "brief_today" in keys(db)


def test_records_that_prevent_a_repeat_are_kept_one_day_longer(db):
    """A reel is still in YouTube's feed the next day, and results are looked for from yesterday onward."""
    fill(db)
    cleanup_old_deliveries(SETTINGS, db, ist(9, 9))
    kept = {r.message_type for r in db.list_deliveries(limit=500) if r.message_key.endswith("_yesterday")}
    assert kept == {"VIDEO_POST", "ACTUAL_RESULT", "LIVE_ALERT", "HIGH_ALERT"} and kept <= set(TWO_DAY_TYPES)
    # The morning after, they go too.
    cleanup_old_deliveries(SETTINGS, db, ist(10, 8, 20))
    assert keys(db) == []


def test_a_forwarded_video_is_too_old_to_repeat_by_the_time_its_record_is_deleted():
    from src import social
    published_latest = ist(8, 23, 59)                                # posted late in the day
    record_deleted_at = ist(10, 8, 15)                               # two mornings later
    assert record_deleted_at - published_latest > social.FORWARD_MAX_AGE
    published_earliest_same_day = ist(8, 0, 1)
    assert ist(9, 23, 59) - published_earliest_same_day < timedelta(days=2)   # and its record is still there all next day


def test_the_day_is_the_india_day_not_the_utc_day(db):
    db.save_delivery(record("late_last_night", "MARKET_PULSE", ist(8, 23, 0)))       # 17:30 UTC on the 8th
    db.save_delivery(record("just_after_midnight", "TRADER_CORNER", ist(9, 0, 30)))  # 19:00 UTC on the 8th, but today in India
    cleanup_old_deliveries(SETTINGS, db, ist(9, 9))
    assert keys(db) == ["just_after_midnight"]
