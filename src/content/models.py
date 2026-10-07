"""A generated message: presentation output only. Nothing in it is written back to the database."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

MORNING_UPDATE = "MORNING_UPDATE"
HIGH_ALERT = "HIGH_ALERT"
ACTUAL_RESULT = "ACTUAL_RESULT"
UPCOMING_REMINDER = "UPCOMING_REMINDER"
MESSAGE_TYPES = (MORNING_UPDATE, HIGH_ALERT, ACTUAL_RESULT, UPCOMING_REMINDER)


@dataclass
class Message:
    message_key: str  # stable identity, for later delivery de-duplication
    message_type: str
    event_id: str | None  # None for a message covering several events
    event_name: str | None  # Forex Factory's exact event name -- never the headline
    headline: str | None  # generated presentation text -- never used as the event name
    priority: str | None  # highest Step 2 priority among the events covered
    highlight_required: bool
    text: str  # ready to publish
    generated_at: str
    # Every event the message covers, each with its own canonical name and headline.
    events: list[dict] = field(default_factory=list)
    attribution: str | None = None  # notice a source requires when its figures are shown
    markdown_safe: bool = True  # False if a value would unbalance *bold* / _italic_; send as plain text then

    def to_dict(self) -> dict:
        return asdict(self)
