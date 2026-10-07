"""Deterministic event classification, driven entirely by config/gold_priority_rules.json.

No event names, weights or thresholds live in this file. It only knows how
to read the rule file and apply it.
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .models import GOLD_LEVELS, HIGH, LOW, MODERATE, NONE, PRIORITIES, STRONG, WEAK, Classification

# Rule-file sections that assign a Gold relevance level, in evaluation order.
_LEVEL_SECTIONS = (
    ("exclusions", NONE),
    ("strong_gold_events", STRONG),
    ("moderate_gold_events", MODERATE),
    ("weak_gold_events", WEAK),
)
_IMPORTANCE_SECTIONS = (("critical_events", "critical_event"), ("high_priority_events", "high_priority_event"))
_IMPORTANCE_LABELS = {
    "critical_event": "critical event",
    "high_priority_event": "high-priority event",
    "other": "no special event weighting",
}


class RulesConfigError(ValueError):
    """The rule file is missing, unreadable or inconsistent."""


def _normalize(name: str) -> str:
    return " ".join(name.split()).casefold()


def _compile(pattern: str) -> re.Pattern:
    """Whole-name, case-insensitive match; * and ? are the only wildcards."""
    escaped = "".join(c if c in "*?" else f"[{c}]" if c in "[]" else c for c in _normalize(pattern))
    return re.compile(fnmatch.translate(escaped))


@dataclass(frozen=True)
class _Rule:
    pattern: str
    regex: re.Pattern
    section: str
    level: str
    category: str
    reason: str


class PriorityRules:
    """Applies the rule file to (currency, event name, Forex Factory impact)."""

    def __init__(self, config: dict):
        try:
            self._load(config)
        except (KeyError, TypeError, AttributeError) as exc:
            raise RulesConfigError(f"Rule file is missing or has a malformed entry: {exc!r}") from exc

    @classmethod
    def from_file(cls, path: str | Path) -> "PriorityRules":
        try:
            config = json.loads(Path(path).read_text(encoding="utf-8"))
        except OSError as exc:
            raise RulesConfigError(f"Cannot read the classification rule file {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise RulesConfigError(f"The classification rule file {path} is not valid JSON: {exc}") from exc
        if not isinstance(config, dict):
            raise RulesConfigError(f"The classification rule file {path} must contain a JSON object.")
        return cls(config)

    # -- loading and validation ---------------------------------------------------

    def _load(self, config: dict) -> None:
        self.version = str(config["classification_version"]).strip()
        if not self.version:
            raise RulesConfigError("classification_version must not be empty.")
        self.currencies = {c.upper() for c in config.get("currencies", ["USD"])}
        self.categories = tuple(config["categories"])
        if len(set(self.categories)) != len(self.categories) or not self.categories:
            raise RulesConfigError("'categories' must be a non-empty list without duplicates.")

        self.default_category = config["default"]["category"]
        self.default_reason = config["default"]["reason"]
        self._check_category(self.default_category, "default")

        self._rules: list[_Rule] = []
        seen: dict[str, str] = {}
        for section, level in _LEVEL_SECTIONS:
            for group in config.get(section, []):
                self._check_category(group["category"], section)
                if not str(group["reason"]).strip():
                    raise RulesConfigError(f"A group in '{section}' has no reason.")
                for pattern in group["events"]:
                    key = _normalize(self._check_pattern(pattern, section))
                    if key in seen:
                        raise RulesConfigError(
                            f"'{pattern}' is listed in both '{seen[key]}' and '{section}'; keep it in one place.")
                    seen[key] = section
                    self._rules.append(_Rule(pattern, _compile(pattern), section, level, group["category"], group["reason"]))

        self._importance = [
            (label, [_compile(self._check_pattern(p, section)) for p in config.get(section, {}).get("events", [])])
            for section, label in _IMPORTANCE_SECTIONS
        ]

        scoring = config["scoring"]
        self._impact_points = {str(k).casefold(): int(v) for k, v in scoring["impact_points"].items()}
        self._relevance_points = {level: int(scoring["gold_relevance_points"][level]) for level in GOLD_LEVELS}
        self._importance_points = {
            label: int(scoring["importance_points"][label]) for label in ("critical_event", "high_priority_event", "other")
        }
        thresholds = scoring["priority_thresholds"]
        self._thresholds = [(p, int(thresholds[p])) for p in PRIORITIES if p != LOW]
        if [t for _, t in self._thresholds] != sorted((t for _, t in self._thresholds), reverse=True):
            raise RulesConfigError("priority_thresholds must decrease from CRITICAL to HIGH to MEDIUM.")
        self._caps = dict(scoring.get("max_priority_by_gold_relevance", {}))
        for level, cap in self._caps.items():
            if level not in GOLD_LEVELS or cap not in PRIORITIES:
                raise RulesConfigError(f"Invalid max_priority_by_gold_relevance entry: {level} -> {cap}")

        highlight = config.get("highlight", {})
        self._highlight_priorities = set(highlight.get("priorities", []))
        if not self._highlight_priorities <= set(PRIORITIES):
            raise RulesConfigError("highlight.priorities contains an unknown priority.")
        self._highlight_events = [
            _compile(self._check_pattern(p, "highlight.high_priority_events"))
            for p in highlight.get("high_priority_events", [])
        ]

    def _check_category(self, category: str, where: str) -> None:
        if category not in self.categories:
            raise RulesConfigError(f"Unknown category '{category}' in '{where}'; add it to 'categories' first.")

    @staticmethod
    def _check_pattern(pattern: object, where: str) -> str:
        if not isinstance(pattern, str) or not pattern.strip():
            raise RulesConfigError(f"'{where}' contains an empty or non-text event name.")
        return pattern

    # -- classification -------------------------------------------------------------

    def match(self, event_name: str | None) -> _Rule | None:
        """The rule that decides this event's relevance and category, if any."""
        if not event_name or not event_name.strip():
            return None
        name = _normalize(event_name)
        return next((rule for rule in self._rules if rule.regex.match(name)), None)

    def _importance_of(self, name: str) -> str:
        for label, patterns in self._importance:
            if any(p.match(name) for p in patterns):
                return label
        return "other"

    def classify(self, event_id: str, currency: str | None, event_name: str | None, impact: str | None) -> Classification:
        name = _normalize(event_name or "")
        rule = self.match(event_name)
        if rule is None:
            level, category = NONE, self.default_category
            relevance_reason = self.default_reason if name else "The event has no name, so no rule can apply."
        else:
            level, category, relevance_reason = rule.level, rule.category, rule.reason
        if (currency or "").upper() not in self.currencies:
            level = NONE
            relevance_reason = f"Only {'/'.join(sorted(self.currencies))} events are assessed for Gold relevance."

        importance = self._importance_of(name) if level != NONE else "other"
        impact_points = self._impact_points.get((impact or "none").casefold(), 0)
        relevance_points = self._relevance_points[level]
        importance_points = self._importance_points[importance]
        score = max(0, min(100, impact_points + relevance_points + importance_points))

        priority = next((p for p, minimum in self._thresholds if score >= minimum), LOW)
        cap = self._caps.get(level)
        capped = cap is not None and PRIORITIES.index(priority) < PRIORITIES.index(cap)
        if capped:
            priority = cap

        highlight = level != NONE and (
            priority in self._highlight_priorities
            or (priority == HIGH and any(p.match(name) for p in self._highlight_events))
        )

        reason = (
            f"Priority {priority}: score {score}/100 = Forex Factory impact {impact or 'None'} ({impact_points}) "
            f"+ Gold relevance {level} ({relevance_points}) + {_IMPORTANCE_LABELS[importance]} ({importance_points})."
        )
        if capped:
            reason += f" Capped at {cap} because Gold relevance is {level}."
        if highlight and priority not in self._highlight_priorities:
            reason += " Highlighted by an explicit rule for this event."

        return Classification(
            event_id=event_id,
            gold_relevance=level != NONE,
            gold_relevance_level=level,
            gold_relevance_reason=relevance_reason,
            category=category,
            priority=priority,
            priority_score=score,
            highlight_required=highlight,
            classification_reason=reason,
            classification_version=self.version,
        )
