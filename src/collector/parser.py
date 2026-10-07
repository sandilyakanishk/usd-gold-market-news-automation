"""Turns the raw Forex Factory JSON export into normalized Event objects."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from .models import (
    IMPACT_HIGH,
    IMPACT_HOLIDAY,
    IMPACT_LOW,
    IMPACT_MEDIUM,
    IMPACT_NONE,
    SOURCE_NAME,
    Event,
)

log = logging.getLogger(__name__)

_IMPACT_MAP = {
    "high": IMPACT_HIGH,
    "medium": IMPACT_MEDIUM,
    "low": IMPACT_LOW,
    "holiday": IMPACT_HOLIDAY,
}


class MalformedFeedError(Exception):
    """The feed as a whole is unusable (not JSON, or not a list of events)."""


@dataclass
class ParseResult:
    events: list[Event] = field(default_factory=list)
    skipped: int = 0  # individual records dropped because they were malformed


def normalize_impact(raw: object) -> str:
    """Map Forex Factory's impact label onto High / Medium / Low / Holiday / None."""
    if not isinstance(raw, str):
        return IMPACT_NONE
    return _IMPACT_MAP.get(raw.strip().lower(), IMPACT_NONE)


def _clean(value: object) -> str | None:
    """Empty / missing source values become None; nothing is invented."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def make_event_id(raw_date: str, currency: str, event_name: str, occurrence: int = 1) -> str:
    """Deterministic ID. The feed has no event ID, so we hash its identifying fields.

    The raw source timestamp is used (not the display-timezone one) so the ID
    does not change when DISPLAY_TIMEZONE changes.
    """
    key = f"{raw_date}|{currency}|{event_name}"
    if occurrence > 1:
        key += f"|#{occurrence}"
    return "ff-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def _format_offset(dt: datetime) -> str:
    offset = dt.utcoffset()
    total = int(offset.total_seconds() // 60)
    sign = "+" if total >= 0 else "-"
    return f"UTC{sign}{abs(total) // 60:02d}:{abs(total) % 60:02d}"


def _resolve_time(raw_date: str, display_tz: ZoneInfo | None) -> tuple[str, str, str | None, str | None]:
    """Return (date, time, timezone, datetime_utc). Raises ValueError if unparseable."""
    dt = datetime.fromisoformat(raw_date)
    if dt.tzinfo is None:
        # No offset in the source: keep the wall-clock values, do not guess a zone.
        return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M"), None, None
    utc = dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if display_tz is not None:
        local = dt.astimezone(display_tz)
        return local.strftime("%Y-%m-%d"), local.strftime("%H:%M"), display_tz.key, utc
    return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M"), _format_offset(dt), utc


def parse_feed(
    text: str,
    *,
    retrieved_at: str,
    source_url: str,
    display_tz: ZoneInfo | None = None,
    gold_classifier: Callable[[str, str], bool] | None = None,
) -> ParseResult:
    """Parse the full feed (all currencies). Bad records are skipped and counted."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise MalformedFeedError(f"Feed is not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise MalformedFeedError(f"Expected a JSON list of events, got {type(data).__name__}")

    result = ParseResult()
    seen: dict[str, int] = {}
    for index, record in enumerate(data):
        try:
            if not isinstance(record, dict):
                raise ValueError("record is not an object")
            name = _clean(record.get("title"))
            currency = _clean(record.get("country"))
            raw_date = _clean(record.get("date"))
            if not name or not currency or not raw_date:
                raise ValueError("missing title, country or date")
            date, time, tz_label, utc = _resolve_time(raw_date, display_tz)
        except ValueError as exc:
            result.skipped += 1
            log.warning("Skipping malformed feed record #%d: %s (%r)", index, exc, record)
            continue

        currency = currency.upper()
        # Two source rows with identical identifying fields get distinct IDs.
        base_key = f"{raw_date}|{currency}|{name}"
        seen[base_key] = seen.get(base_key, 0) + 1
        original_impact = _clean(record.get("impact"))

        result.events.append(
            Event(
                event_id=make_event_id(raw_date, currency, name, seen[base_key]),
                date=date,
                time=time,
                timezone=tz_label,
                datetime_utc=utc,
                currency=currency,
                event_name=name,
                impact=normalize_impact(original_impact),
                original_impact=original_impact,
                gold_relevance=bool(gold_classifier(currency, name)) if gold_classifier else False,
                forecast=_clean(record.get("forecast")),
                previous=_clean(record.get("previous")),
                actual=_clean(record.get("actual")),  # not in the export today; kept in case it is added
                source=SOURCE_NAME,
                source_url=source_url,
                retrieved_at=retrieved_at,
            )
        )
    return result
