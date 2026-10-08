"""Collect -> parse -> filter -> store. The single entry point later stages will call."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .collector.forex_factory import FetchError, fetch_calendar
from .collector.parser import parse_feed
from .config import Settings
from .database.base import EventRepository
from .filters.gold_usd_filters import GoldRelevance

log = logging.getLogger(__name__)


@dataclass
class FeedPayload:
    text: str
    retrieved_at: str
    from_cache: bool
    warning: str | None = None  # set when a live fetch failed and an older copy was used


@dataclass
class SyncResult:
    feed_events: int  # everything in the raw feed, all currencies
    stored_events: int  # events kept after the USD / Gold filter
    inserted: int
    updated: int
    unchanged: int
    removed: int
    skipped: int
    from_cache: bool
    retrieved_at: str
    warning: str | None = None


def _mtime_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_feed(settings: Settings, *, force: bool = False) -> FeedPayload:
    """Return the raw feed, hitting the network at most once per min_fetch_interval.

    The raw response is kept on disk both as a rate-limit-friendly cache and
    as the debugging copy of the unfiltered source data.
    """
    cache = settings.raw_cache_path
    if not force and cache.is_file():
        age = time.time() - cache.stat().st_mtime
        if age < settings.min_fetch_interval_seconds:
            log.info("Using cached feed (%.0fs old)", age)
            return FeedPayload(cache.read_text(encoding="utf-8"), _mtime_iso(cache), from_cache=True)

    try:
        text = fetch_calendar(settings.feed_url, timeout=settings.request_timeout_seconds, user_agent=settings.user_agent)
    except FetchError as exc:
        if cache.is_file():
            warning = f"Live fetch failed ({exc}); showing data retrieved at {_mtime_iso(cache)}"
            log.warning(warning)
            return FeedPayload(cache.read_text(encoding="utf-8"), _mtime_iso(cache), from_cache=True, warning=warning)
        raise

    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(text, encoding="utf-8")
    log.info("Fetched feed from %s (%d bytes)", settings.feed_url, len(text))
    return FeedPayload(text, _mtime_iso(cache), from_cache=False)


def sync(settings: Settings, db: EventRepository, *, force: bool = False) -> SyncResult:
    """Fetch the calendar and bring the database in line with it."""
    payload = load_feed(settings, force=force)
    classifier = GoldRelevance.from_file(settings.gold_config_path)
    display_tz = ZoneInfo(settings.display_timezone) if settings.display_timezone else None

    parsed = parse_feed(
        payload.text,
        retrieved_at=payload.retrieved_at,
        source_url=settings.feed_url,
        display_tz=display_tz,
        gold_classifier=classifier,
    )
    # Only events in the configured currencies (USD) are stored; other
    # currencies stay available in the raw cache file.
    relevant = [e for e in parsed.events if e.currency in classifier.currencies]
    counts = db.upsert_events(relevant)

    removed = 0
    instants = [e.datetime_utc for e in parsed.events if e.datetime_utc]
    if instants:
        removed = db.delete_missing((e.event_id for e in relevant), min(instants), max(instants))

    result = SyncResult(
        feed_events=len(parsed.events),
        stored_events=len(relevant),
        removed=removed,
        skipped=parsed.skipped,
        from_cache=payload.from_cache,
        retrieved_at=payload.retrieved_at,
        warning=payload.warning,
        **counts,
    )
    log.info("Sync complete: %s", result)
    return result


def cleanup_old_events(settings: Settings, db: EventRepository, today: date | None = None) -> int:
    """Delete calendar events older than the retention window. Returns how many were removed.

    "Today" is taken in the application timezone, the same one the stored
    event dates use, so both backends apply the identical cutoff.
    """
    if today is None:
        tz = ZoneInfo(settings.display_timezone) if settings.display_timezone else timezone.utc
        today = datetime.now(tz).date()
    cutoff = today - timedelta(days=settings.calendar_retention_days)
    removed = db.cleanup_old_events(cutoff.isoformat())
    log.info("Retention cleanup: removed %d event(s) dated before %s (retention %d days)",
             removed, cutoff.isoformat(), settings.calendar_retention_days)
    cleanup_old_deliveries(settings, db)
    return removed


# A market pulse belongs to one half hour that never comes back, so its record is only needed briefly.
PULSE_RECORD_DAYS = 2
PULSE_MESSAGE_TYPE = "MARKET_PULSE"


def cleanup_old_deliveries(settings: Settings, db: EventRepository, now: datetime | None = None) -> int:
    """Delete "this was sent" records that can no longer prevent a duplicate. Returns how many were removed.

    A record must outlive whatever could make the same message come up again:
    events are kept for calendar_retention_days and videos are forwarded only
    while they are a few days old, so the general window is never shorter
    than the calendar's plus one day.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    days = max(settings.delivery_retention_days, settings.calendar_retention_days + 1)
    stamp = "%Y-%m-%dT%H:%M:%SZ"
    removed = db.cleanup_old_deliveries((now - timedelta(days=PULSE_RECORD_DAYS)).strftime(stamp), only_type=PULSE_MESSAGE_TYPE)
    removed += db.cleanup_old_deliveries((now - timedelta(days=days)).strftime(stamp), except_type=PULSE_MESSAGE_TYPE)
    log.info("Retention cleanup: removed %d delivery record(s) (pulse %d days, others %d days)", removed, PULSE_RECORD_DAYS, days)
    return removed
