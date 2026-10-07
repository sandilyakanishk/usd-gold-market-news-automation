"""Enrichment record: where an event's Actual came from and how it compares with the forecast.

The Actual value itself lives in events.actual. This record only describes it,
and is linked to the Forex Factory event through event_id.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

UPCOMING, RELEASED, NO_DATA, FAILED = "UPCOMING", "RELEASED", "NO_DATA", "FAILED"
RELEASE_STATUSES = (UPCOMING, RELEASED, NO_DATA, FAILED)

ABOVE_FORECAST = "ABOVE_FORECAST"
BELOW_FORECAST = "BELOW_FORECAST"
IN_LINE_WITH_FORECAST = "IN_LINE_WITH_FORECAST"
NOT_AVAILABLE = "NOT_AVAILABLE"
SURPRISE_STATUSES = (ABOVE_FORECAST, BELOW_FORECAST, IN_LINE_WITH_FORECAST, NOT_AVAILABLE)


@dataclass
class ActualRecord:
    event_id: str
    release_status: str  # UPCOMING / RELEASED / NO_DATA / FAILED
    status_reason: str = ""  # why there is no value (empty when released or simply upcoming)
    actual_source: str | None = None  # provider that supplied the value, e.g. "BLS"
    actual_source_event: str | None = None  # the provider's series, e.g. "CUSR0000SA0: CPI-U ..."
    actual_period: str | None = None  # period the value refers to, e.g. "2026-09"
    actual_revision: int = 0  # 0 = no value yet, 1 = first release, 2+ = revised
    surprise_status: str = NOT_AVAILABLE  # factual Actual-vs-Forecast comparison, not a signal
    surprise_value: float | None = None  # Actual minus Forecast, in the unit both are quoted in
    actual_retrieved_at: str | None = None  # when a value was first obtained
    actual_updated_at: str | None = None  # when the value last changed
    updated_at: str | None = None  # when this record last changed

    def to_dict(self) -> dict:
        return asdict(self)
