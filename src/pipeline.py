"""Collect -> parse -> filter -> store. The single entry point later stages will call."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from datetime import time as clock_time
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


# Records that must outlive the day they were made, because the thing they describe can still come up the
# next day: a reel stays in YouTube's feed, a result is looked for from yesterday onward, a stream can run on.
TWO_DAY_TYPES = ("HIGH_ALERT", "ACTUAL_RESULT", "UPCOMING_REMINDER", "VIDEO_POST", "LIVE_ALERT", "NEWS_COUNTDOWN")
# The day's first message goes out at this time; the previous day's records are deleted from then on.
CLEANUP_FROM = clock_time(8, 15)


def cleanup_old_deliveries(settings: Settings, db: EventRepository, now: datetime | None = None) -> int:
    """Delete the previous days' "this was sent" records. Returns how many were removed.

    Nothing is deleted during a day. From the morning's first message onward,
    records made before today are deleted, except the TWO_DAY_TYPES, which
    are kept one day longer. Only the database is touched, never Telegram.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    tz = ZoneInfo(settings.display_timezone) if settings.display_timezone else timezone.utc
    local = now.astimezone(tz)
    if local.time() < CLEANUP_FROM:
        return 0
    today = datetime.combine(local.date(), clock_time(0), tzinfo=tz).astimezone(timezone.utc)
    stamp = "%Y-%m-%dT%H:%M:%SZ"
    removed = db.cleanup_old_deliveries(today.strftime(stamp), except_types=TWO_DAY_TYPES)
    removed += db.cleanup_old_deliveries((today - timedelta(days=1)).strftime(stamp))
    log.info("Daily cleanup: removed %d delivery record(s) made before today", removed)
    return removed
