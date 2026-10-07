"""Loads, validates and renders config/message_templates.json."""

from __future__ import annotations

import json
import re
import string
from pathlib import Path

from ..actuals.models import ABOVE_FORECAST, BELOW_FORECAST, IN_LINE_WITH_FORECAST
from ..classification.models import GOLD_LEVELS, PRIORITIES
from .models import ACTUAL_RESULT, HIGH_ALERT, MORNING_UPDATE, UPCOMING_REMINDER

# Every name a template line may use for one event.
EVENT_VARIABLES = (
    "headline", "headline_text", "event_name", "currency", "currency_flag",
    "date", "weekday", "time", "display_time",
    "impact", "impact_label", "gold_relevance", "gold_relevance_level", "gold_label",
    "category", "category_label", "priority", "priority_label", "priority_score", "highlight_required",
    "forecast", "previous", "actual", "release_status", "actual_source", "actual_period", "actual_revision",
    "surprise_status", "surprise_label", "surprise_value", "revision_note",
    "classification_reason", "gold_relevance_reason", "source", "attribution",
)
# Names available to the header, footer and empty-day text of the daily update.
DAILY_VARIABLES = ("date", "weekday", "event_count", "source")

_SECTIONS = {
    MORNING_UPDATE: {"header": DAILY_VARIABLES, "event": EVENT_VARIABLES, "event_separator": (),
                     "empty": DAILY_VARIABLES, "footer": DAILY_VARIABLES},
    HIGH_ALERT: {"body": EVENT_VARIABLES},
    ACTUAL_RESULT: {"body": EVENT_VARIABLES},
    UPCOMING_REMINDER: {"body": EVENT_VARIABLES},
}
_BLANK_RUN = re.compile(r"\n{3,}")


class TemplateConfigError(ValueError):
    """The template file is missing, unreadable or inconsistent."""


def _fields(line: str) -> list[str]:
    return [name for _, name, _, _ in string.Formatter().parse(line) if name is not None]


def markdown_safe(text: str) -> bool:
    """True when *bold* and _italic_ markers are balanced and nothing else could be read as markup.

    Telegram's simple Markdown rejects a message with an unmatched marker, so
    a message that fails this check should be sent as plain text.
    """
    return text.count("*") % 2 == 0 and text.count("_") % 2 == 0 and "`" not in text and "[" not in text


class MessageTemplates:
    def __init__(self, config: dict):
        try:
            self._load(config)
        except (KeyError, TypeError, AttributeError) as exc:
            raise TemplateConfigError(f"Template file is missing or has a malformed entry: {exc!r}") from exc

    @classmethod
    def from_file(cls, path: str | Path) -> "MessageTemplates":
        try:
            config = json.loads(Path(path).read_text(encoding="utf-8"))
        except OSError as exc:
            raise TemplateConfigError(f"Cannot read the message template file {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise TemplateConfigError(f"The message template file {path} is not valid JSON: {exc}") from exc
        if not isinstance(config, dict):
            raise TemplateConfigError(f"The message template file {path} must contain a JSON object.")
        return cls(config)

    def _load(self, config: dict) -> None:
        self.version = str(config["templates_version"])
        self.missing = str(config.get("missing_value", "-"))

        time = config["time"]
        self.time_format, self.date_format = str(time["time_format"]), str(time["date_format"])
        self.day_month_format = str(time.get("day_month_format", "%d %b"))
        self.timezone_labels = dict(time.get("timezone_labels", {}))

        selection = config["selection"]
        self.daily_minimum_priority = self._priority(selection["daily_minimum_priority"], "daily_minimum_priority")
        self.daily_require_gold = bool(selection.get("daily_require_gold_relevance", True))
        self.alert_requires_highlight = bool(selection.get("alert_requires_highlight", True))
        self.alert_only_before_release = bool(selection.get("alert_only_before_release", True))
        self.upcoming_minimum_priority = self._priority(selection["upcoming_minimum_priority"], "upcoming_minimum_priority")
        self.upcoming_require_gold = bool(selection.get("upcoming_require_gold_relevance", True))

        labels = config["labels"]
        self.labels = {name: dict(labels[name]) for name in
                       ("currency_flag", "impact", "gold_relevance_level", "priority", "surprise_status")}
        for name, required in (("gold_relevance_level", GOLD_LEVELS), ("priority", PRIORITIES),
                               ("surprise_status", (ABOVE_FORECAST, BELOW_FORECAST, IN_LINE_WITH_FORECAST))):
            missing = [key for key in required if not str(self.labels[name].get(key, "")).strip()]
            if missing:
                raise TemplateConfigError(f"labels.{name} has no text for: {', '.join(missing)}")
        self.yes, self.no = str(labels.get("yes", "YES")), str(labels.get("no", "NO"))
        self.revision_note = str(labels.get("revision_note", ""))
        if set(_fields(self.revision_note)) - {"actual_revision"}:
            raise TemplateConfigError("labels.revision_note may only use {actual_revision}.")

        sources = config["sources"]
        self.calendar_source = str(sources["calendar"])
        self.attribution = dict(sources.get("attribution", {}))

        self.omit_if_missing = set(config.get("omit_line_if_missing", []))
        unknown = self.omit_if_missing - set(EVENT_VARIABLES)
        if unknown:
            raise TemplateConfigError(f"omit_line_if_missing lists unknown name(s): {', '.join(sorted(unknown))}")

        self._templates: dict[str, dict[str, list[str]]] = {}
        for message_type, sections in _SECTIONS.items():
            given = config["templates"][message_type]
            self._templates[message_type] = {}
            for section, allowed in sections.items():
                lines = given[section]
                if not isinstance(lines, list) or not all(isinstance(line, str) for line in lines):
                    raise TemplateConfigError(f"templates.{message_type}.{section} must be a list of text lines.")
                for line in lines:
                    try:
                        used = set(_fields(line))
                    except ValueError as exc:
                        raise TemplateConfigError(
                            f"templates.{message_type}.{section}: malformed line {line!r} ({exc})") from exc
                    if used - set(allowed):
                        raise TemplateConfigError(
                            f"templates.{message_type}.{section}: unknown placeholder(s) "
                            f"{', '.join('{' + n + '}' for n in sorted(used - set(allowed)))} in {line!r}")
                self._templates[message_type][section] = lines
        for message_type in (HIGH_ALERT, ACTUAL_RESULT, UPCOMING_REMINDER):
            self._require_names(message_type, "body")
        self._require_names(MORNING_UPDATE, "event")

    def _require_names(self, message_type: str, section: str) -> None:
        """Every event-based template must show both the canonical event name and the headline."""
        used = {name for line in self._templates[message_type][section] for name in _fields(line)}
        if "event_name" not in used:
            raise TemplateConfigError(f"templates.{message_type}.{section} must include {{event_name}}.")
        if not used & {"headline", "headline_text"}:
            raise TemplateConfigError(f"templates.{message_type}.{section} must include {{headline}} or {{headline_text}}.")

    @staticmethod
    def _priority(value: str, where: str) -> str:
        if value not in PRIORITIES:
            raise TemplateConfigError(f"selection.{where} must be one of {', '.join(PRIORITIES)}.")
        return value

    # -- rendering ----------------------------------------------------------------

    def render(self, message_type: str, section: str, values: dict[str, str | None]) -> str:
        """Fill one template section. `values` maps names to text, or None when the value is missing."""
        out = []
        for line in self._templates[message_type][section]:
            used = _fields(line)
            if any(name in self.omit_if_missing and values.get(name) is None for name in used):
                continue
            filled = {name: (values.get(name) if values.get(name) is not None else self.missing) for name in used}
            out.append(line.format(**filled))
        return "\n".join(out)

    @staticmethod
    def tidy(text: str) -> str:
        """Collapse the blank runs left behind by dropped lines."""
        return _BLANK_RUN.sub("\n\n", text).strip("\n")
