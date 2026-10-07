"""Classification record: derived, editorial metadata about a stored event.

It never replaces or edits the Forex Factory source record; it points at it
through event_id.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

STRONG, MODERATE, WEAK, NONE = "STRONG", "MODERATE", "WEAK", "NONE"
GOLD_LEVELS = (STRONG, MODERATE, WEAK, NONE)

CRITICAL, HIGH, MEDIUM, LOW = "CRITICAL", "HIGH", "MEDIUM", "LOW"
PRIORITIES = (CRITICAL, HIGH, MEDIUM, LOW)  # most to least important


@dataclass
class Classification:
    event_id: str
    gold_relevance: bool  # True for every level except NONE
    gold_relevance_level: str  # STRONG / MODERATE / WEAK / NONE
    gold_relevance_reason: str
    category: str
    priority: str  # CRITICAL / HIGH / MEDIUM / LOW -- editorial, not a trading signal
    priority_score: int  # 0-100, how the priority was reached
    highlight_required: bool
    classification_reason: str
    classification_version: str
    classified_at: str | None = None  # last time the rules were applied
    updated_at: str | None = None  # last time the result changed

    def to_dict(self) -> dict:
        return asdict(self)
