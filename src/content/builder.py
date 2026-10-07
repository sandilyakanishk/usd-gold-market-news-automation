"""Builds ready-to-publish messages from stored events, classifications and actual results.

Read-only: it receives data and returns text. It never writes to the
database, and it never alters an event name -- the headline is a separate,
generated field.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from ..actuals.models import NOT_AVAILABLE, RELEASED
from ..actuals.service import EnrichedEvent, is_due
from ..actuals.surprise import parse_value
from ..classification.models import GOLD_LEVELS, PRIORITIES
from .headlines import HeadlineRules
from .models import ACTUAL_RESULT, HIGH_ALERT, MORNING_UPDATE, UPCOMING_REMINDER, Message
from .templates import MessageTemplates, markdown_safe


def _at_least(priority: str | None, minimum: str) -> bool:
    return priority in PRIORITIES and PRIORITIES.index(priority) <= PRIORITIES.index(minimum)


def clock_face(moment: datetime) -> str:
    """The clock emoji closest to a time: on the hour or on the half hour."""
    minutes = moment.hour * 60 + moment.minute + 15           # round to the nearest half hour
    hour, half = (minutes // 60) % 12, (minutes % 60) >= 30
    return chr((0x1F55C if half else 0x1F550) + (hour - 1) % 12)


class ContentBuilder:
    def __init__(self, templates: MessageTemplates, headlines: HeadlineRules, *,
                 display_timezone: str | None, now: datetime):
        self.templates, self.headlines = templates, headlines
        self.tz = ZoneInfo(display_timezone) if display_timezone else timezone.utc
        self.tz_name = display_timezone or "UTC"
        self.now = now.astimezone(timezone.utc)
        self.generated_at = self.now.strftime("%Y-%m-%dT%H:%M:%SZ")

    # -- per-event values -------------------------------------------------------------

    def local_time(self, item: EnrichedEvent) -> datetime | None:
        """The event's release time in the display timezone, from the stored UTC instant."""
        event = item.event
        if event.datetime_utc:
            return datetime.fromisoformat(event.datetime_utc.replace("Z", "+00:00")).astimezone(self.tz)
        if event.date and event.time:
            return datetime.fromisoformat(f"{event.date}T{event.time}")  # zone unknown: shown as stored
        return None

    def _timezone_label(self, moment: datetime) -> str:
        if moment.tzinfo is None:
            return ""
        return self.templates.timezone_labels.get(self.tz_name) or moment.tzname() or ""

    def headline_for(self, item: EnrichedEvent) -> str:
        event, record, c = item.event, item.record, item.classification
        local = self.local_time(item)
        days_ahead = (local.date() - self.now.astimezone(self.tz).date()).days if local else 0
        when = self.headlines.when_phrase(days_ahead, self._text(local, self.templates.day_month_format) if local else "")
        state = self.headlines.state(
            event_name=event.event_name, due=is_due(event, self.now), release_status=record.release_status,
            surprise_status=record.surprise_status, actual=event.actual, previous=event.previous)
        missing = self.templates.missing
        return self.headlines.headline(
            event_name=event.event_name, category=c.category if c else None, state=state, when=when,
            actual=event.actual or missing, forecast=event.forecast or missing, previous=event.previous or missing)

    def _text(self, moment: datetime, pattern: str) -> str:
        """Format a date or time, optionally without leading zeros ("2:00 PM", "8 October")."""
        text = moment.strftime(pattern)
        return text.lstrip("0") or "0" if self.templates.strip_leading_zeros else text

    def variables(self, item: EnrichedEvent, number: int | None = None) -> dict[str, str | None]:
        """Everything a template line may show for one event. None marks a missing value."""
        event, record, c, t = item.event, item.record, item.classification, self.templates
        local = self.local_time(item)
        headline = self.headline_for(item)
        # The same headline without its leading emoji, for templates that supply their own.
        first, _, rest = headline.partition(" ")
        headline_text = rest if rest and not any(ch.isalnum() for ch in first) else headline

        display_time = None
        if local:
            display_time = " ".join(filter(None, [self._text(local, t.time_format), self._timezone_label(local)]))

        surprise_known = record.release_status == RELEASED and record.surprise_status != NOT_AVAILABLE
        surprise_value = None
        if surprise_known and record.surprise_value is not None:
            unit = (parse_value(event.actual) or (None, ""))[1]
            surprise_value = f"{record.surprise_value:+g}{unit if unit != '%' else ' pts'}"
        released = record.release_status == RELEASED
        why = None
        if c:
            why = t.why_it_matters.get(c.category) or t.why_it_matters.get("default") or c.gold_relevance_reason

        return {
            "headline": headline,
            "headline_text": headline_text,
            "event_name": event.event_name,  # exactly as Forex Factory supplied it
            "currency": event.currency,
            "currency_flag": t.labels["currency_flag"].get(event.currency, event.currency),
            "number": (t.numbers[number - 1] if number <= len(t.numbers) else f"{number}.") if number else None,
            "clock": clock_face(local) if local else None,
            # Display-only scale; the Step 2 priority score is a separate value and is not derived from it.
            "gold_relevance_score": str(t.gold_relevance_score[c.gold_relevance_level]) if c else None,
            "why_it_matters": why,
            "alert_title": t.alert_titles.get(event.impact, t.alert_titles["default"]),
            "result_sentence": t.result_sentences.get(record.surprise_status) if surprise_known else None,
            "date": self._text(local, t.date_format) if local else event.date,
            "weekday": local.strftime("%A") if local else None,
            "time": local.strftime("%H:%M") if local else event.time,
            "display_time": display_time,
            "impact": event.impact,
            "impact_label": t.labels["impact"].get(event.impact, event.impact.upper()),
            "gold_relevance": (t.yes if c.gold_relevance else t.no) if c else None,
            "gold_relevance_level": c.gold_relevance_level if c else None,
            "gold_label": t.labels["gold_relevance_level"][c.gold_relevance_level] if c else None,
            "category": c.category if c else None,
            "category_label": c.category.replace("_", " ") if c else None,
            "priority": c.priority if c else None,
            "priority_label": t.labels["priority"][c.priority] if c else None,
            "priority_score": str(c.priority_score) if c else None,
            "highlight_required": (t.yes if c.highlight_required else t.no) if c else None,
            "forecast": event.forecast,
            "previous": event.previous,
            "actual": event.actual,
            "release_status": record.release_status,
            "actual_source": record.actual_source if released else None,
            "actual_period": record.actual_period if released else None,
            "actual_revision": str(record.actual_revision) if released else None,
            "surprise_status": record.surprise_status.replace("_", " ") if surprise_known else None,
            "surprise_label": t.labels["surprise_status"][record.surprise_status] if surprise_known else None,
            "surprise_value": surprise_value,
            "revision_note": (t.revision_note.format(actual_revision=record.actual_revision) or None)
            if released and record.actual_revision > 1 else None,
            "classification_reason": c.classification_reason if c else None,
            "gold_relevance_reason": c.gold_relevance_reason if c else None,
            "source": t.calendar_source,
            "attribution": t.attribution.get(record.actual_source) if released and record.actual_source else None,
        }

    # -- messages ---------------------------------------------------------------------

    def _event_message(self, message_type: str, key: str, item: EnrichedEvent) -> Message:
        values = self.variables(item)
        text = self.templates.tidy(self.templates.render(message_type, "body", values))
        c = item.classification
        return Message(
            message_key=key, message_type=message_type, event_id=item.event.event_id,
            event_name=item.event.event_name, headline=values["headline"],
            priority=c.priority if c else None, highlight_required=bool(c and c.highlight_required),
            text=text, generated_at=self.generated_at,
            events=[{"event_id": item.event.event_id, "event_name": item.event.event_name, "headline": values["headline"]}],
            attribution=values["attribution"], markdown_safe=markdown_safe(text),
        )

    def morning_update(self, items: list[EnrichedEvent], day: date) -> Message:
        """One overview message for `day` (a date in the display timezone)."""
        t = self.templates
        chosen = []
        for item in items:
            local, c = self.local_time(item), item.classification
            if not local or local.date() != day or c is None:
                continue
            if not _at_least(c.priority, t.daily_minimum_priority):
                continue
            if t.daily_require_gold and not c.gold_relevance:
                continue
            rank = (PRIORITIES.index(c.priority), GOLD_LEVELS.index(c.gold_relevance_level)) \
                if t.daily_order == "priority" else (0, 0)
            chosen.append((rank, local.replace(tzinfo=None), item.event.event_name, item))
        # Presentation order only: most important first, then by time. Stored data is not reordered.
        chosen.sort(key=lambda entry: entry[:3])
        chosen_items = [entry[3] for entry in chosen]

        day_moment = datetime(day.year, day.month, day.day)
        day_values = {
            "date": self._text(day_moment, t.date_format), "weekday": day.strftime("%A"),
            "event_count": str(len(chosen_items)), "source": t.calendar_source,
        }
        blocks, covered = [], []
        for position, item in enumerate(chosen_items, 1):
            values = self.variables(item, number=position)
            blocks.append(t.render(MORNING_UPDATE, "event", values))
            covered.append({"event_id": item.event.event_id, "event_name": item.event.event_name,
                            "headline": values["headline"]})
        separator = "\n" + t.render(MORNING_UPDATE, "event_separator", {}) + "\n"
        middle = separator.join(blocks) if blocks else t.render(MORNING_UPDATE, "empty", day_values)
        text = t.tidy("\n".join([t.render(MORNING_UPDATE, "header", day_values), middle,
                                 t.render(MORNING_UPDATE, "footer", day_values)]))
        priorities = [i.classification.priority for i in chosen_items]
        return Message(
            message_key=f"DAILY_UPDATE_{day.isoformat()}", message_type=MORNING_UPDATE, event_id=None,
            event_name=None, headline=None,
            priority=min(priorities, key=PRIORITIES.index) if priorities else None,
            highlight_required=any(i.classification.highlight_required for i in chosen_items),
            text=text, generated_at=self.generated_at, events=covered, markdown_safe=markdown_safe(text),
        )

    def high_alerts(self, items: list[EnrichedEvent]) -> list[Message]:
        """One alert per event whose classification says highlight_required."""
        t = self.templates
        messages = []
        for item in items:
            c = item.classification
            if c is None or (t.alert_requires_highlight and not c.highlight_required):
                continue
            if t.alert_only_before_release and is_due(item.event, self.now):
                continue
            messages.append(self._event_message(HIGH_ALERT, f"HIGH_ALERT_{item.event.event_id}", item))
        return messages

    def actual_results(self, items: list[EnrichedEvent]) -> list[Message]:
        """One message per event with a verified Actual. Nothing is produced for any other status."""
        return [
            self._event_message(ACTUAL_RESULT, f"ACTUAL_{item.event.event_id}_{item.record.actual_revision}", item)
            for item in items
            if item.record.release_status == RELEASED and item.event.actual
        ]

    def upcoming_reminders(self, items: list[EnrichedEvent]) -> list[Message]:
        """One reminder per important event that has not been released yet."""
        t = self.templates
        messages = []
        for item in items:
            c = item.classification
            if c is None or is_due(item.event, self.now):
                continue
            if not _at_least(c.priority, t.upcoming_minimum_priority):
                continue
            if t.upcoming_require_gold and not c.gold_relevance:
                continue
            messages.append(self._event_message(UPCOMING_REMINDER, f"UPCOMING_{item.event.event_id}", item))
        return messages


def build_content_builder(settings, now: datetime | None = None) -> ContentBuilder:
    return ContentBuilder(
        MessageTemplates.from_file(settings.message_templates_path),
        HeadlineRules.from_file(settings.headline_rules_path),
        display_timezone=settings.display_timezone,
        now=now or datetime.now(timezone.utc),
    )
