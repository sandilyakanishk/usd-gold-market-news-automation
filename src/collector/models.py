"""Normalized event model shared by the parser, database and filters."""

from __future__ import annotations

from dataclasses import asdict, dataclass

SOURCE_NAME = "Forex Factory"

IMPACT_HIGH = "High"
IMPACT_MEDIUM = "Medium"
IMPACT_LOW = "Low"
IMPACT_HOLIDAY = "Holiday"
IMPACT_NONE = "None"


@dataclass
class Event:
    event_id: str
    date: str | None  # YYYY-MM-DD in `timezone`
    time: str | None  # HH:MM in `timezone`
    timezone: str | None  # IANA name or "UTC±HH:MM"; None when the source gave no offset
    datetime_utc: str | None  # exact instant, ISO 8601 with Z; None when timezone is unknown
    currency: str
    event_name: str
    impact: str  # normalized: High / Medium / Low / Holiday / None
    original_impact: str | None  # exactly as Forex Factory sent it
    gold_relevance: bool  # our own classification, independent of `impact`
    forecast: str | None
    previous: str | None
    actual: str | None
    source: str
    source_url: str
    retrieved_at: str
    updated_at: str | None = None

    @property
    def is_high_impact(self) -> bool:
        return self.impact == IMPACT_HIGH

    def to_dict(self) -> dict:
        return asdict(self)
