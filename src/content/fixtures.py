"""Sample events for previewing templates. Nothing here touches a database."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from ..actuals.models import NO_DATA, RELEASED, UPCOMING, ActualRecord
from ..actuals.service import EnrichedEvent, is_due
from ..actuals.surprise import compare
from ..classification.rules import PriorityRules
from ..collector.models import SOURCE_NAME, Event
from ..collector.parser import make_event_id


def make_item(entry: dict, *, rules: PriorityRules, display_timezone: str | None, now: datetime) -> EnrichedEvent:
    """Build one sample event, classified by the real Step 2 rules and compared by the real Step 3 logic."""
    when_utc = entry["release_utc"]
    instant = datetime.fromisoformat(when_utc.replace("Z", "+00:00"))
    local = instant.astimezone(ZoneInfo(display_timezone)) if display_timezone else instant
    currency = entry.get("currency", "USD")
    event = Event(
        event_id=make_event_id(when_utc, currency, entry["name"]),
        date=local.strftime("%Y-%m-%d"), time=local.strftime("%H:%M"), timezone=display_timezone or "UTC",
        datetime_utc=instant.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        currency=currency, event_name=entry["name"],
        impact=entry.get("impact", "None"), original_impact=entry.get("impact"), gold_relevance=False,
        forecast=entry.get("forecast"), previous=entry.get("previous"), actual=entry.get("actual"),
        source=SOURCE_NAME, source_url="sample-fixture", retrieved_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    classification = rules.classify(event.event_id, event.currency, event.event_name, event.impact)
    if event.actual:
        status, value = compare(event.actual, event.forecast)
        stamp = event.datetime_utc
        record = ActualRecord(
            event.event_id, RELEASED, actual_source=entry.get("actual_source"),
            actual_source_event=entry.get("actual_source_event"), actual_period=entry.get("actual_period"),
            actual_revision=int(entry.get("actual_revision", 1)), surprise_status=status, surprise_value=value,
            actual_retrieved_at=stamp, actual_updated_at=stamp, updated_at=stamp)
    elif is_due(event, now):
        record = ActualRecord(event.event_id, NO_DATA, "Sample event without a released figure.")
    else:
        record = ActualRecord(event.event_id, UPCOMING)
    return EnrichedEvent(event, record, classification)


def load_preview_fixture(settings) -> tuple[list[EnrichedEvent], datetime]:
    """The sample events and the moment the preview should treat as 'now'."""
    data = json.loads(Path(settings.preview_fixture_path).read_text(encoding="utf-8"))
    now = datetime.fromisoformat(data["reference_time_utc"].replace("Z", "+00:00"))
    rules = PriorityRules.from_file(settings.priority_rules_path)
    items = [make_item(e, rules=rules, display_timezone=settings.display_timezone, now=now) for e in data["events"]]
    return items, now
