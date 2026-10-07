"""Applies the rules to stored events and keeps the results in the database."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

from ..collector.models import Event
from ..config import Settings
from ..database.base import EventRepository
from .models import Classification
from .rules import PriorityRules

log = logging.getLogger(__name__)


@dataclass
class ClassifyResult:
    events: int
    inserted: int
    updated: int
    unchanged: int
    version: str
    unmatched: list[str]  # event names that fell through to the default rule


@dataclass
class ClassifiedEvent:
    event: Event
    classification: Classification | None  # None until --classify has covered the event

    def to_dict(self) -> dict:
        data = self.event.to_dict()
        data["classification"] = self.classification.to_dict() if self.classification else None
        return data


def classify_events(settings: Settings, db: EventRepository, now: str | None = None) -> ClassifyResult:
    """Classify every stored event. Safe to repeat: one row per event, changed only when the result changes."""
    rules = PriorityRules.from_file(settings.priority_rules_path)
    events = db.query_events()
    classifications = [rules.classify(e.event_id, e.currency, e.event_name, e.impact) for e in events]
    counts = db.upsert_classifications(classifications, now)
    unmatched = sorted({e.event_name for e in events if rules.match(e.event_name) is None})
    result = ClassifyResult(len(events), version=rules.version, unmatched=unmatched, **counts)
    log.info("Classification complete: %s", result)
    return result


def load_classified(
    db: EventRepository,
    *,
    priorities: Iterable[str] | None = None,
    gold_only: bool = False,
    highlight_only: bool = False,
    **event_filters,
) -> list[ClassifiedEvent]:
    """Stored events with their classification, optionally narrowed by classification fields.

    `event_filters` are passed to EventRepository.query_events (dates, impacts, currency).
    """
    events = db.query_events(**event_filters)
    found = db.get_classifications([e.event_id for e in events])
    wanted = set(priorities) if priorities else None
    rows = []
    for event in events:
        c = found.get(event.event_id)
        if wanted is not None and (c is None or c.priority not in wanted):
            continue
        if gold_only and not (c and c.gold_relevance):
            continue
        if highlight_only and not (c and c.highlight_required):
            continue
        rows.append(ClassifiedEvent(event, c))
    return rows
