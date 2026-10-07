"""Deterministic headline generation from config/headline_rules.json.

A headline is presentation. It is derived from an event; it never becomes,
replaces or alters the event's name.
"""

from __future__ import annotations

import json
import re
import string
from dataclasses import dataclass
from pathlib import Path

from ..actuals.models import ABOVE_FORECAST, BELOW_FORECAST, IN_LINE_WITH_FORECAST, NOT_AVAILABLE, RELEASED
from ..actuals.surprise import compare

UPCOMING, PASSED = "UPCOMING", "PASSED"
STATES = (UPCOMING, PASSED, RELEASED, ABOVE_FORECAST, BELOW_FORECAST, IN_LINE_WITH_FORECAST)
_COMPARED = (ABOVE_FORECAST, BELOW_FORECAST, IN_LINE_WITH_FORECAST)
_FIELDS = {"flag", "subject", "short", "verb", "when", "actual", "forecast", "previous"}


class HeadlineConfigError(ValueError):
    """The headline rule file is missing, unreadable or inconsistent."""


def _normalize(name: str) -> str:
    return " ".join(name.split()).casefold()


def _compile(pattern: str) -> re.Pattern:
    """Whole-name, case-insensitive match. Each * becomes a capture group, usable as {1}, {2}..."""
    parts = [("(.*?)" if c == "*" else re.escape(c)) for c in " ".join(pattern.split())]
    return re.compile("".join(parts), re.IGNORECASE)


def _fields(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


@dataclass(frozen=True)
class _Rule:
    regex: re.Pattern
    captures: int
    subject: str | None
    short: str | None
    verb: str
    headlines: dict[str, str]
    compare_to: str  # "forecast" or "previous"


class HeadlineRules:
    def __init__(self, config: dict):
        try:
            self._load(config)
        except (KeyError, TypeError, AttributeError) as exc:
            raise HeadlineConfigError(f"Headline rule file is missing or has a malformed entry: {exc!r}") from exc

    @classmethod
    def from_file(cls, path: str | Path) -> "HeadlineRules":
        try:
            config = json.loads(Path(path).read_text(encoding="utf-8"))
        except OSError as exc:
            raise HeadlineConfigError(f"Cannot read the headline rule file {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise HeadlineConfigError(f"The headline rule file {path} is not valid JSON: {exc}") from exc
        if not isinstance(config, dict):
            raise HeadlineConfigError(f"The headline rule file {path} must contain a JSON object.")
        return cls(config)

    def _load(self, config: dict) -> None:
        self.version = str(config["headline_rules_version"])
        self.flag = str(config.get("flag", ""))
        self._when = dict(config["when"])
        for key in ("today", "tomorrow", "other"):
            if not str(self._when[key]).strip():
                raise HeadlineConfigError(f"'when.{key}' must not be empty.")
        self._patterns = {state: str(config["patterns"][state]) for state in STATES}
        for state, text in self._patterns.items():
            self._check_text(text, f"patterns.{state}", captures=0)
        default_verb = str(config.get("default_verb", "comes in"))

        self._rules: list[_Rule] = []
        seen: set[str] = set()
        for entry in config["rules"]:
            headlines = {k: str(v) for k, v in (entry.get("headlines") or {}).items()}
            unknown = set(headlines) - set(STATES)
            if unknown:
                raise HeadlineConfigError(f"Unknown headline state(s) {sorted(unknown)} for {entry['events']}.")
            compare_to = entry.get("compare_to", "forecast")
            if compare_to not in ("forecast", "previous"):
                raise HeadlineConfigError(f"compare_to must be 'forecast' or 'previous' for {entry['events']}.")
            has_subject = bool(entry.get("subject")) and bool(entry.get("short"))
            if not has_subject and not {UPCOMING, PASSED} <= set(headlines):
                raise HeadlineConfigError(
                    f"{entry['events']}: give 'subject' and 'short', or 'headlines' for at least UPCOMING and PASSED.")
            for pattern in entry["events"]:
                if not isinstance(pattern, str) or not pattern.strip():
                    raise HeadlineConfigError("A headline rule contains an empty event name.")
                key = _normalize(pattern)
                if key in seen:
                    raise HeadlineConfigError(f"'{pattern}' has more than one headline rule.")
                seen.add(key)
                captures = pattern.count("*")
                for state, text in headlines.items():
                    self._check_text(text, f"{pattern} / {state}", captures)
                self._rules.append(_Rule(_compile(pattern), captures, entry.get("subject"), entry.get("short"),
                                         str(entry.get("verb", default_verb)), headlines, compare_to))

        self._fallback = {k: v for k, v in config.get("category_fallback", {}).items() if not k.startswith("_")}
        self._default = config["default"]
        for where, entry in [("default", self._default), *self._fallback.items()]:
            if not entry.get("subject") or not entry.get("short"):
                raise HeadlineConfigError(f"'{where}' needs both 'subject' and 'short'.")
        self._default_verb = default_verb

    @staticmethod
    def _check_text(text: str, where: str, captures: int) -> None:
        if not text.strip():
            raise HeadlineConfigError(f"Empty headline text at {where}.")
        try:
            names = _fields(text)
        except ValueError as exc:
            raise HeadlineConfigError(f"Malformed headline text at {where}: {exc}") from exc
        allowed = _FIELDS | {str(i) for i in range(1, captures + 1)}
        if names - allowed:
            raise HeadlineConfigError(f"Unknown placeholder(s) {sorted(names - allowed)} in headline text at {where}.")

    # -- generation -----------------------------------------------------------------

    def state(self, *, event_name: str, due: bool, release_status: str | None, surprise_status: str | None,
              actual: str | None, previous: str | None) -> str:
        """Which headline applies: upcoming, passed without a figure, or released (and how it compared)."""
        if release_status == RELEASED and actual:
            rule = self._match(event_name)[0]
            if rule is not None and rule.compare_to == "previous":
                status = compare(actual, previous)[0]
            else:
                status = surprise_status or NOT_AVAILABLE
            return status if status in _COMPARED else RELEASED
        return PASSED if due else UPCOMING

    def _match(self, event_name: str) -> tuple[_Rule | None, tuple[str, ...]]:
        name = " ".join((event_name or "").split())
        for rule in self._rules:
            found = rule.regex.fullmatch(name)
            if found:
                return rule, tuple(g.strip() for g in found.groups())
        return None, ()

    def when_phrase(self, days_ahead: int, day_month: str) -> str:
        if days_ahead == 0:
            return self._when["today"]
        if days_ahead == 1:
            return self._when["tomorrow"]
        return self._when["other"].format(day_month=day_month)

    def headline(self, *, event_name: str, category: str | None, state: str, when: str,
                 actual: str = "", forecast: str = "", previous: str = "") -> str:
        """The headline text for an event in the given state. Always non-empty, never the event name."""
        rule, groups = self._match(event_name)
        values = {"flag": self.flag, "when": when, "actual": actual, "forecast": forecast, "previous": previous}
        template = None
        if rule is not None:
            template = rule.headlines.get(state)
            if template is None and state in _COMPARED:
                template = rule.headlines.get(RELEASED)
            if template is None and not rule.subject:
                # A rule with its own wording and no subject: fall back to its nearest state.
                template = rule.headlines[PASSED if state != UPCOMING else UPCOMING]
        if template is None:
            source = rule if (rule is not None and rule.subject) else None
            fallback = self._fallback.get(category or "", self._default)
            values.update(
                subject=source.subject if source else fallback["subject"],
                short=source.short if source else fallback["short"],
                verb=source.verb if source else fallback.get("verb", self._default_verb),
            )
            template = self._patterns[state]
        else:
            values.update(subject=rule.subject or "", short=rule.short or "", verb=rule.verb)
        # {1}, {2}... are the texts matched by * in the rule's event name.
        return " ".join(template.format("", *groups, **values).split())
