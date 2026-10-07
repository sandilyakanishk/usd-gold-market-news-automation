"""Explicit mapping from Forex Factory events to official data series (config/actual_event_mapping.json)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from ..classification.rules import _compile, _normalize

FREQUENCIES = {
    "monthly": ("previous_month",),
    "quarterly": ("previous_quarter",),
    "weekly": ("previous_saturday",),
    "daily": ("day_after_release", "release_day"),
}
TRANSFORMS = ("level", "change", "percent_change")


class MappingConfigError(ValueError):
    """The mapping file is missing, unreadable or inconsistent."""


def shift_months(day: date, months: int) -> date:
    index = day.year * 12 + (day.month - 1) + months
    return date(index // 12, index % 12 + 1, 1)


@dataclass(frozen=True)
class EventMapping:
    forex_factory_events: tuple[str, ...]
    provider: str
    series_id: str
    series_name: str
    frequency: str
    reference_period: str
    transform: str
    periods: int
    scale: Decimal
    decimals: int
    suffix: str

    @property
    def source_event(self) -> str:
        return f"{self.series_id}: {self.series_name}"

    def period_for(self, release_date: date) -> date:
        """The observation date this release reports on (first day of the month/quarter, or the day itself)."""
        if self.reference_period == "previous_month":
            return shift_months(release_date, -1)
        if self.reference_period == "previous_quarter":
            quarter_start = date(release_date.year, (release_date.month - 1) // 3 * 3 + 1, 1)
            return shift_months(quarter_start, -3)
        if self.reference_period == "previous_saturday":
            return release_date - timedelta(days=(release_date.weekday() - 5) % 7 or 7)
        if self.reference_period == "day_after_release":
            return release_date + timedelta(days=1)
        return release_date

    def base_period(self, period: date) -> date | None:
        """The earlier observation a change is measured from (monthly series only)."""
        return None if self.transform == "level" else shift_months(period, -self.periods)

    def period_label(self, period: date) -> str:
        if self.frequency == "monthly":
            return period.strftime("%Y-%m")
        if self.frequency == "quarterly":
            return f"{period.year}-Q{(period.month - 1) // 3 + 1}"
        return period.isoformat()

    def compute(self, observations: dict[date, Decimal], period: date) -> str | None:
        """The Actual as Forex Factory would print it, or None if the provider has not published the period."""
        current = observations.get(period)
        if current is None:
            return None
        if self.transform == "level":
            value = current
        else:
            base = observations.get(self.base_period(period))
            if base is None:
                return None
            if self.transform == "change":
                value = current - base
            elif base == 0:
                return None
            else:
                value = (current / base - 1) * 100
        rounded = (value * self.scale).quantize(Decimal(1).scaleb(-self.decimals), rounding=ROUND_HALF_UP)
        if rounded == 0:
            rounded = abs(rounded)  # never "-0.0"
        return f"{rounded}{self.suffix}"


class ActualEventMapping:
    def __init__(self, config: dict):
        try:
            self._load(config)
        except (KeyError, TypeError, AttributeError, ArithmeticError) as exc:
            raise MappingConfigError(f"Mapping file is missing or has a malformed entry: {exc!r}") from exc

    @classmethod
    def from_file(cls, path: str | Path) -> "ActualEventMapping":
        try:
            config = json.loads(Path(path).read_text(encoding="utf-8"))
        except OSError as exc:
            raise MappingConfigError(f"Cannot read the actual-value mapping file {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise MappingConfigError(f"The actual-value mapping file {path} is not valid JSON: {exc}") from exc
        if not isinstance(config, dict):
            raise MappingConfigError(f"The actual-value mapping file {path} must contain a JSON object.")
        return cls(config)

    def _load(self, config: dict) -> None:
        self.version = str(config["mapping_version"])
        self.currency = str(config.get("currency", "USD")).upper()
        self.providers: dict[str, dict] = dict(config["providers"])
        self._no_actual = [_compile(p) for p in config.get("events_without_actual", {}).get("events", [])]
        self._by_name: dict[str, EventMapping] = {}
        for entry in config["mappings"]:
            mapping = EventMapping(
                forex_factory_events=tuple(entry["forex_factory_events"]),
                provider=entry["provider"],
                series_id=entry["series_id"],
                series_name=entry["series_name"],
                frequency=entry["frequency"],
                reference_period=entry["reference_period"],
                transform=entry["transform"],
                periods=int(entry.get("periods", 1)),
                scale=Decimal(str(entry.get("scale", 1))),
                decimals=int(entry["decimals"]),
                suffix=str(entry.get("suffix", "")),
            )
            self._validate(mapping)
            for name in mapping.forex_factory_events:
                key = _normalize(name)
                if not key or "*" in key or "?" in key:
                    raise MappingConfigError(f"'{name}': mapped event names must be exact, without wildcards.")
                if key in self._by_name:
                    raise MappingConfigError(f"'{name}' is mapped more than once.")
                if any(p.match(key) for p in self._no_actual):
                    raise MappingConfigError(f"'{name}' is both mapped and listed under events_without_actual.")
                self._by_name[key] = mapping

    def _validate(self, m: EventMapping) -> None:
        where = "/".join(m.forex_factory_events) or m.series_id
        if not m.forex_factory_events:
            raise MappingConfigError(f"Mapping for series {m.series_id} lists no Forex Factory event.")
        if m.provider not in self.providers:
            raise MappingConfigError(f"'{where}': unknown provider '{m.provider}'.")
        if m.reference_period not in FREQUENCIES.get(m.frequency, ()):
            raise MappingConfigError(f"'{where}': reference_period '{m.reference_period}' does not fit frequency '{m.frequency}'.")
        if m.transform not in TRANSFORMS:
            raise MappingConfigError(f"'{where}': unknown transform '{m.transform}'.")
        if m.transform != "level" and (m.frequency != "monthly" or m.periods < 1):
            raise MappingConfigError(f"'{where}': '{m.transform}' needs a monthly series and periods of 1 or more.")
        if not 0 <= m.decimals <= 6 or not m.series_id.strip():
            raise MappingConfigError(f"'{where}': invalid decimals or series_id.")

    def find(self, currency: str | None, event_name: str | None) -> EventMapping | None:
        """The mapping for exactly this event, or None. Only the configured currency is ever matched."""
        if not event_name or (currency or "").upper() != self.currency:
            return None
        return self._by_name.get(_normalize(event_name))

    def has_no_actual(self, event_name: str | None) -> bool:
        return bool(event_name) and any(p.match(_normalize(event_name)) for p in self._no_actual)

    @property
    def mappings(self) -> list[EventMapping]:
        return list(dict.fromkeys(self._by_name.values()))
