"""Old delivery records are deleted automatically so the database stays small."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.database.database import SQLiteRepository
from src.delivery.models import DeliveryRecord
from src.pipeline import cleanup_old_deliveries, cleanup_old_events

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
SETTINGS = SimpleNamespace(delivery_retention_days=30, calendar_retention_days=14, display_timezone="Asia/Kolkata")


def record(key, message_type, age_days, status="SENT"):
    stamp = (NOW - timedelta(days=age_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
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


def test_pulse_records_go_after_two_days_and_the_rest_after_thirty(db):
    for item in [record("PULSE_fresh", "MARKET_PULSE", 1), record("PULSE_old", "MARKET_PULSE", 3),
                 record("BRIEF_recent", "MORNING_UPDATE", 29), record("BRIEF_old", "MORNING_UPDATE", 31),
                 record("VIDEO_recent", "VIDEO_POST", 10), record("VIDEO_old", "VIDEO_POST", 45),
                 record("FAILED_old", "HIGH_ALERT", 40, status="FAILED")]:
        db.save_delivery(item)
    assert cleanup_old_deliveries(SETTINGS, db, NOW) == 4
    assert keys(db) == ["BRIEF_recent", "PULSE_fresh", "VIDEO_recent"]
    assert cleanup_old_deliveries(SETTINGS, db, NOW) == 0            # nothing more to remove


def test_records_always_outlive_the_calendar(db):
    """However low the setting, a record is kept longer than the events that could produce the message again."""
    short = SimpleNamespace(delivery_retention_days=1, calendar_retention_days=14, display_timezone="Asia/Kolkata")
    db.save_delivery(record("ALERT_10_days", "HIGH_ALERT", 10))
    db.save_delivery(record("ALERT_16_days", "HIGH_ALERT", 16))
    cleanup_old_deliveries(short, db, NOW)
    assert keys(db) == ["ALERT_10_days"]


def test_the_regular_cleanup_after_each_sync_includes_delivery_records(db):
    old = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    item = record("BRIEF_ancient", "MORNING_UPDATE", 0)
    item.sent_at = item.created_at = item.updated_at = old
    db.save_delivery(item)
    recent = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh = record("BRIEF_now", "MORNING_UPDATE", 0)
    fresh.sent_at = fresh.created_at = fresh.updated_at = recent
    db.save_delivery(fresh)
    cleanup_old_events(SETTINGS, db)
    assert "BRIEF_ancient" not in keys(db) and "BRIEF_now" in keys(db)
