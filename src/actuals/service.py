"""Actual-result enrichment: attach the released value to stored Forex Factory events.

Forex Factory remains the source of the event (identity, timing, impact,
forecast, previous). A provider is asked for one thing only: the released
Actual of an event that has an explicit mapping.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from ..classification.models import Classification
from ..collector.models import Event
from ..config import Settings
from ..database.base import EventRepository
from .mapping import ActualEventMapping, EventMapping, shift_months
from .models import FAILED, NO_DATA, NOT_AVAILABLE, RELEASED, UPCOMING, ActualRecord
from .providers import ActualDataProvider, ProviderError, SeriesRequest, build_providers
from .surprise import compare

log = logging.getLogger(__name__)

REASON_NO_NUMERIC_RESULT = "This kind of event has no numeric result."
REASON_NOT_MAPPED = "No verified free source is mapped for this event."
REASON_TOO_OLD = "No value was published by the mapped source within the retry window."


@dataclass
class EnrichedEvent:
    """One event with its stored classification and its (new or existing) enrichment record."""
    event: Event
    record: ActualRecord
    classification: Classification | None = None
    change: str = "unchanged"  # inserted / updated / unchanged, relative to the database

    def to_dict(self) -> dict:
        data = self.event.to_dict()
        data["actual_result"] = self.record.to_dict()
        data["classification"] = self.classification.to_dict() if self.classification else None
        return data


@dataclass
class EnrichResult:
    rows: list[EnrichedEvent] = field(default_factory=list)
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    actuals_written: int = 0  # events whose Actual value was set or revised
    provider_calls: dict[str, int] = field(default_factory=dict)
    dry_run: bool = False


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_due(event: Event, now: datetime) -> bool:
    """Has the event's release time passed? Uses the exact UTC instant stored by the collector."""
    if event.datetime_utc:
        return _utc(event.datetime_utc) <= now
    # No known instant: only treat it as due once its calendar date is clearly over everywhere.
    return bool(event.date) and date.fromisoformat(event.date) < (now - timedelta(days=1)).date()


def _release_date(event: Event) -> date:
    """The release date in UTC, the reference for choosing the reporting period."""
    return _utc(event.datetime_utc).date() if event.datetime_utc else date.fromisoformat(event.date)


def _series_request(mapping: EventMapping, period: date) -> SeriesRequest:
    base = mapping.base_period(period)
    # One extra month of margin costs nothing and keeps year boundaries simple.
    return SeriesRequest(mapping.series_id, shift_months(base or period, -1) if base else period, period)


def enrich_actuals(
    settings: Settings,
    db: EventRepository,
    *,
    providers: dict[str, ActualDataProvider] | None = None,
    now: datetime | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    event_ids: set[str] | None = None,
    recheck_released: bool = False,
    dry_run: bool = False,
) -> EnrichResult:
    """Bring the release status and Actual of stored events up to date.

    Safe to call repeatedly: nothing is written unless something changed,
    upcoming events never trigger a provider request, and released events are
    left alone unless `recheck_released` asks for a revision check.
    """
    now = now or datetime.now(timezone.utc)
    stamp = _stamp(now)
    mapping_file = ActualEventMapping.from_file(settings.actual_mapping_path)
    providers = providers if providers is not None else build_providers(settings)

    events = [e for e in db.query_events(currency=mapping_file.currency, date_from=date_from, date_to=date_to)
              if event_ids is None or e.event_id in event_ids]
    existing = db.get_actual_records([e.event_id for e in events])
    classifications = db.get_classifications([e.event_id for e in events])

    decided: dict[str, ActualRecord] = {}
    new_values: dict[str, str] = {}  # event_id -> Actual to store in events.actual
    pending: dict[str, list[tuple[Event, EventMapping, date]]] = {}

    for event in events:
        old = existing.get(event.event_id)
        if not is_due(event, now):
            decided[event.event_id] = ActualRecord(event.event_id, UPCOMING)
        elif old and old.release_status == RELEASED and not recheck_released:
            decided[event.event_id] = old
        elif mapping_file.has_no_actual(event.event_name):
            decided[event.event_id] = ActualRecord(event.event_id, NO_DATA, REASON_NO_NUMERIC_RESULT)
        else:
            mapping = mapping_file.find(event.currency, event.event_name)
            if mapping is None:
                decided[event.event_id] = ActualRecord(event.event_id, NO_DATA, REASON_NOT_MAPPED)
            elif not (old and old.release_status == RELEASED) and \
                    _release_date(event) < (now - timedelta(days=settings.actuals_retry_days)).date():
                decided[event.event_id] = ActualRecord(event.event_id, NO_DATA, REASON_TOO_OLD)
            else:
                pending.setdefault(mapping.provider, []).append((event, mapping, mapping.period_for(_release_date(event))))

    result = EnrichResult(dry_run=dry_run)
    for provider_name, items in pending.items():
        provider = providers.get(provider_name)
        problem = f"Provider '{provider_name}' is not available." if provider is None else provider.unavailable_reason()
        observations, failure = {}, None
        if problem is None:
            try:
                result.provider_calls[provider_name] = result.provider_calls.get(provider_name, 0) + 1
                observations = provider.fetch([_series_request(m, p) for _, m, p in items])
            except ProviderError as exc:
                failure = str(exc)
                log.warning("Actuals: %s failed: %s", provider_name, failure)

        for event, mapping, period in items:
            old = existing.get(event.event_id)
            was_released = bool(old and old.release_status == RELEASED)
            if problem is not None:
                new = ActualRecord(event.event_id, NO_DATA, problem)
            elif failure is not None:
                new = ActualRecord(event.event_id, FAILED, failure)
            else:
                value = mapping.compute(observations.get(mapping.series_id, {}), period)
                if value is None:
                    new = ActualRecord(
                        event.event_id, NO_DATA,
                        f"{provider_name} has not published {mapping.series_id} for {mapping.period_label(period)} yet.")
                else:
                    new, changed_value = _released_record(event, old, mapping, period, value, stamp)
                    if changed_value is not None:
                        new_values[event.event_id] = changed_value
            # A value that was already verified is never downgraded by a later miss or outage.
            decided[event.event_id] = old if (was_released and new.release_status != RELEASED) else new

    writes: list[tuple[ActualRecord, str | None]] = []
    for event in events:
        new, old = decided[event.event_id], existing.get(event.event_id)
        new_actual = new_values.get(event.event_id)
        if old is None:
            change = "inserted"
        elif _content(new) != _content(old) or new_actual is not None:
            change = "updated"
        else:
            change = "unchanged"
        new.updated_at = old.updated_at if (old and change == "unchanged") else stamp
        if change != "unchanged":
            writes.append((new, new_actual))
        setattr(result, change, getattr(result, change) + 1)
        result.actuals_written += new_actual is not None
        shown = event if new_actual is None else Event(**{**event.to_dict(), "actual": new_actual})
        result.rows.append(EnrichedEvent(shown, new, classifications.get(event.event_id), change))
        if change != "unchanged":
            log.info("Actuals: %s %s (%s) -> %s%s%s", "would set" if dry_run else "set", event.event_name,
                     event.event_id, new.release_status,
                     f" actual={new_actual} source={new.actual_source}" if new_actual else "",
                     f" [{new.status_reason}]" if new.status_reason else "")

    if writes and not dry_run:
        db.save_actual_results(writes, stamp)
    log.info("Actuals %s: %d events, %d inserted, %d updated, %d unchanged, %d actual value(s) written, provider calls %s",
             "dry run" if dry_run else "run", len(events), result.inserted, result.updated, result.unchanged,
             result.actuals_written, result.provider_calls or "none")
    return result


def _content(record: ActualRecord) -> dict:
    return {k: v for k, v in record.to_dict().items() if k != "updated_at"}


def _released_record(event: Event, old: ActualRecord | None, mapping: EventMapping, period: date,
                     value: str, stamp: str) -> tuple[ActualRecord, str | None]:
    """Record for a verified value, plus the value itself when it is new or revised."""
    was_released = bool(old and old.release_status == RELEASED)
    if was_released and event.actual == value:
        return old, None  # nothing changed: keep every timestamp as it is
    status, difference = compare(value, event.forecast)
    record = ActualRecord(
        event_id=event.event_id,
        release_status=RELEASED,
        actual_source=mapping.provider,
        actual_source_event=mapping.source_event,
        actual_period=mapping.period_label(period),
        actual_revision=old.actual_revision + 1 if was_released else 1,
        surprise_status=status,
        surprise_value=difference,
        actual_retrieved_at=old.actual_retrieved_at if was_released else stamp,
        actual_updated_at=stamp,
    )
    return record, value


def load_enriched(db: EventRepository, *, now: datetime | None = None, **event_filters) -> list[EnrichedEvent]:
    """Stored events with their enrichment record, without contacting any provider."""
    now = now or datetime.now(timezone.utc)
    events = db.query_events(**event_filters)
    ids = [e.event_id for e in events]
    records, classifications = db.get_actual_records(ids), db.get_classifications(ids)

    def unchecked(event: Event) -> ActualRecord:
        if not is_due(event, now):
            return ActualRecord(event.event_id, UPCOMING)
        return ActualRecord(event.event_id, NO_DATA, "Not checked yet; run --enrich-actuals.", surprise_status=NOT_AVAILABLE)

    return [EnrichedEvent(e, records.get(e.event_id) or unchecked(e), classifications.get(e.event_id)) for e in events]
