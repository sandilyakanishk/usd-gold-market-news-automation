from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.collector.parser import parse_feed
from src.config import PROJECT_ROOT, Settings
from src.database.database import Database
from src.filters.gold_usd_filters import GoldRelevance

FIXTURES = PROJECT_ROOT / "fixtures"
FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
RETRIEVED_AT = "2026-10-07T18:30:27Z"
IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture
def feed_text() -> str:
    """A real export saved on 2026-10-07 (83 events, 23 of them USD)."""
    return (FIXTURES / "ff_calendar_thisweek.json").read_text(encoding="utf-8")


@pytest.fixture
def classifier() -> GoldRelevance:
    return GoldRelevance.from_file(PROJECT_ROOT / "config" / "gold_relevance.json")


@pytest.fixture
def parse(classifier):
    def _parse(text: str, display_tz=IST):
        return parse_feed(text, retrieved_at=RETRIEVED_AT, source_url=FEED_URL,
                          display_tz=display_tz, gold_classifier=classifier)
    return _parse


@pytest.fixture
def db():
    with Database(":memory:") as database:
        yield database


@pytest.fixture
def loaded_db(db, parse, feed_text):
    """Database holding the USD events of the fixture week."""
    db.upsert_events(e for e in parse(feed_text).events if e.currency == "USD")
    return db


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        feed_url=FEED_URL,
        display_timezone="Asia/Kolkata",
        database_path=tmp_path / "events.db",
        raw_cache_path=tmp_path / "raw" / "feed.json",
        gold_config_path=PROJECT_ROOT / "config" / "gold_relevance.json",
        log_path=tmp_path / "collector.log",
        min_fetch_interval_seconds=1800,
        request_timeout_seconds=5,
        user_agent="test-agent",
        log_level="INFO",
    )
