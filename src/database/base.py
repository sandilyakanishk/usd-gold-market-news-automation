"""Backend-independent event storage.

EventRepository holds all the behaviour (upsert, dedup, queries, retention).
A backend only supplies a connection, its schema, and the conversion between
Event values and its native column types.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator

from ..actuals.models import ActualRecord
from ..classification.models import Classification
from ..collector.models import Event
from ..delivery.models import DeliveryRecord

log = logging.getLogger(__name__)

TABLE = "events"

# Columns whose change counts as "the event was updated".
CONTENT_COLUMNS = (
    "date", "time", "timezone", "datetime_utc", "currency", "event_name",
    "impact", "original_impact", "gold_relevance", "forecast", "previous", "actual",
)
ALL_COLUMNS = ("event_id",) + CONTENT_COLUMNS + ("source", "source_url", "retrieved_at", "updated_at")

_SELECT = f"SELECT {', '.join(ALL_COLUMNS)} FROM {TABLE}"
_PLACEHOLDERS = ", ".join("?" for _ in ALL_COLUMNS)
_INSERT = f"INSERT INTO {TABLE} ({', '.join(ALL_COLUMNS)}) VALUES ({_PLACEHOLDERS})"
_UPSERT = (
    f"{_INSERT} ON CONFLICT (event_id) DO UPDATE SET "
    + ", ".join(f"{c} = excluded.{c}" for c in ALL_COLUMNS if c != "event_id")
)
_INSERT_IF_ABSENT = f"{_INSERT} ON CONFLICT (event_id) DO NOTHING"
_CHUNK = 500

CLASSIFICATION_TABLE = "event_classifications"
# Columns whose change counts as "the classification changed".
CLASSIFICATION_CONTENT = (
    "gold_relevance", "gold_relevance_level", "gold_relevance_reason", "category", "priority",
    "priority_score", "highlight_required", "classification_reason", "classification_version",
)
CLASSIFICATION_COLUMNS = ("event_id",) + CLASSIFICATION_CONTENT + ("classified_at", "updated_at")
ACTUALS_TABLE = "event_actuals"
ACTUALS_COLUMNS = (
    "event_id", "release_status", "status_reason", "actual_source", "actual_source_event", "actual_period",
    "actual_revision", "surprise_status", "surprise_value", "actual_retrieved_at", "actual_updated_at", "updated_at",
)
_A_SELECT = f"SELECT {', '.join(ACTUALS_COLUMNS)} FROM {ACTUALS_TABLE}"
_A_UPSERT = (
    f"INSERT INTO {ACTUALS_TABLE} ({', '.join(ACTUALS_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in ACTUALS_COLUMNS)}) ON CONFLICT (event_id) DO UPDATE SET "
    + ", ".join(f"{c} = excluded.{c}" for c in ACTUALS_COLUMNS if c != "event_id")
)
DELIVERIES_TABLE = "message_deliveries"
DELIVERY_COLUMNS = (
    "message_key", "provider", "destination_id", "destination", "message_type", "status",
    "provider_message_id", "sent_at", "error", "attempts", "created_at", "updated_at",
)
_D_SELECT = f"SELECT {', '.join(DELIVERY_COLUMNS)} FROM {DELIVERIES_TABLE}"
_D_KEY = ("message_key", "provider", "destination_id")
# A successful delivery is final: the conflict branch never overwrites a SENT row.
_D_UPSERT = (
    f"INSERT INTO {DELIVERIES_TABLE} ({', '.join(DELIVERY_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in DELIVERY_COLUMNS)}) ON CONFLICT ({', '.join(_D_KEY)}) DO UPDATE SET "
    + ", ".join(f"{c} = excluded.{c}" for c in DELIVERY_COLUMNS if c not in _D_KEY + ("created_at",))
    + f" WHERE {DELIVERIES_TABLE}.status <> 'SENT'"
)
PRICES_TABLE = "price_snapshots"
PRICE_COLUMNS = ("symbol", "slot", "price", "source", "source_updated_at", "recorded_at")
_C_SELECT = f"SELECT {', '.join(CLASSIFICATION_COLUMNS)} FROM {CLASSIFICATION_TABLE}"
_C_UPSERT = (
    f"INSERT INTO {CLASSIFICATION_TABLE} ({', '.join(CLASSIFICATION_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in CLASSIFICATION_COLUMNS)}) ON CONFLICT (event_id) DO UPDATE SET "
    + ", ".join(f"{c} = excluded.{c}" for c in CLASSIFICATION_COLUMNS if c != "event_id")
)


class DatabaseError(Exception):
    """Base class for storage failures. Messages never contain credentials."""


class DatabaseConfigError(DatabaseError):
    """The database settings are missing or invalid."""


class DatabaseConnectionError(DatabaseError):
    """The database could not be reached or refused the login."""


class SchemaMissingError(DatabaseError):
    """The database is reachable but its tables have not been created."""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class EventRepository(ABC):
    """Storage interface used by the pipeline, the filters and the CLI."""

    backend: str = ""
    # Driver exception types that are translated into DatabaseError.
    _driver_errors: tuple[type[BaseException], ...] = ()

    # -- backend hooks ----------------------------------------------------------
    # SQL is written with "?" placeholders; a backend translates if it needs to.

    @abstractmethod
    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        """Run a SELECT and return rows as dicts."""

    @abstractmethod
    def _write(self, sql: str, params: Iterable[Any] = ()) -> int:
        """Run a data-changing statement and return the affected row count."""

    @abstractmethod
    def _commit(self) -> None: ...

    @abstractmethod
    def _rollback(self) -> None: ...

    @abstractmethod
    def init_schema(self) -> None:
        """Create the tables and indexes if they do not exist. Safe to repeat."""

    @abstractmethod
    def describe(self) -> str:
        """Human-readable target for logs, without credentials."""

    @abstractmethod
    def close(self) -> None: ...

    def _encode(self, event: Event) -> dict:
        """Event -> column values in the backend's native types."""
        return event.to_dict()

    def _decode(self, row: dict) -> Event:
        """Stored row -> Event."""
        return Event(**row)

    def _encode_classification(self, classification: Classification) -> dict:
        return classification.to_dict()

    def _decode_classification(self, row: dict) -> Classification:
        return Classification(**row)

    def _encode_actual(self, record: ActualRecord) -> dict:
        return record.to_dict()

    def _decode_actual(self, row: dict) -> ActualRecord:
        return ActualRecord(**row)

    def _encode_delivery(self, record: DeliveryRecord) -> dict:
        return record.to_dict()

    def _decode_delivery(self, row: dict) -> DeliveryRecord:
        return DeliveryRecord(**row)

    def _date_param(self, value: str) -> Any:
        return value

    def _timestamp_param(self, value: str) -> Any:
        return value

    # -- plumbing ---------------------------------------------------------------

    def __enter__(self) -> "EventRepository":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Commit on success; roll back and raise DatabaseError on failure."""
        try:
            yield
            self._commit()
        except DatabaseError:
            self._safe_rollback()
            raise
        except self._driver_errors as exc:
            self._safe_rollback()
            raise self._translate(exc) from exc
        except (ValueError, TypeError) as exc:
            # An event carried a value the column type cannot hold.
            self._safe_rollback()
            raise DatabaseError(f"Malformed event record rejected by the {self.backend} database: {exc}") from exc

    def _safe_rollback(self) -> None:
        try:
            self._rollback()
        except Exception:  # the connection may already be gone
            pass

    def _translate(self, exc: BaseException) -> DatabaseError:
        return DatabaseError(f"{self.backend} database error: {type(exc).__name__}: {exc}")

    def _read(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        try:
            rows = self._query(sql, params)
            self._commit()  # do not leave a read transaction open
            return rows
        except self._driver_errors as exc:
            self._safe_rollback()
            raise self._translate(exc) from exc

    # -- writes -----------------------------------------------------------------

    def _existing(self, event_ids: list[str]) -> dict[str, Event]:
        found: dict[str, Event] = {}
        for start in range(0, len(event_ids), _CHUNK):
            chunk = event_ids[start:start + _CHUNK]
            marks = ", ".join("?" for _ in chunk)
            for row in self._query(f"{_SELECT} WHERE event_id IN ({marks})", chunk):
                event = self._decode(row)
                found[event.event_id] = event
        return found

    def _upsert_one(self, event: Event, existing: Event | None, now: str) -> tuple[str, Event]:
        new = Event(**event.to_dict())
        if existing is None:
            status = "inserted"
            new.updated_at = now
        else:
            if new.actual is None:
                new.actual = existing.actual
            changed = any(getattr(new, c) != getattr(existing, c) for c in CONTENT_COLUMNS)
            status = "updated" if changed else "unchanged"
            new.updated_at = now if changed else existing.updated_at
        values = self._encode(new)
        # ON CONFLICT keeps this safe even if another run inserted the row meanwhile.
        self._write(_UPSERT, [values[c] for c in ALL_COLUMNS])
        return status, new

    def upsert_events(self, events: Iterable[Event], now: str | None = None) -> dict[str, int]:
        """Insert new events and update known ones, as one transaction.

        A known `actual` is never overwritten by a missing one, so a value
        recorded earlier survives later syncs of a feed that omits it.
        """
        events = list(events)
        now = now or utc_now()
        counts = {"inserted": 0, "updated": 0, "unchanged": 0}
        with self._transaction():
            existing = self._existing([e.event_id for e in events])
            for event in events:
                status, stored = self._upsert_one(event, existing.get(event.event_id), now)
                existing[event.event_id] = stored
                counts[status] += 1
        return counts

    def upsert_event(self, event: Event, now: str | None = None) -> str:
        """Insert or update one event. Returns 'inserted', 'updated' or 'unchanged'."""
        counts = self.upsert_events([event], now)
        return next(status for status, n in counts.items() if n)

    def import_events(self, events: Iterable[Event]) -> dict[str, int]:
        """Copy events in exactly as given (timestamps included), skipping IDs already present."""
        counts = {"copied": 0, "already_present": 0}
        with self._transaction():
            for event in events:
                values = self._encode(event)
                if values["updated_at"] is None:
                    values["updated_at"] = values["retrieved_at"]
                copied = self._write(_INSERT_IF_ABSENT, [values[c] for c in ALL_COLUMNS])
                counts["copied" if copied else "already_present"] += 1
        return counts

    def set_actual(self, event_id: str, actual: str | None, now: str | None = None) -> bool:
        """Record a released Actual value on an existing event. False if the ID is unknown."""
        with self._transaction():
            changed = self._write(
                f"UPDATE {TABLE} SET actual = ?, updated_at = ? WHERE event_id = ?",
                (actual, self._timestamp_param(now or utc_now()), event_id),
            )
        return changed > 0

    def delete_missing(self, keep_ids: Iterable[str], start_utc: str, end_utc: str) -> int:
        """Remove events inside [start_utc, end_utc] that the source no longer lists.

        Forex Factory reschedules and withdraws events; without this the old
        row would linger next to the rescheduled one.
        """
        keep = set(keep_ids)
        with self._transaction():
            rows = self._query(
                f"SELECT event_id FROM {TABLE} WHERE datetime_utc IS NOT NULL AND datetime_utc BETWEEN ? AND ?",
                (self._timestamp_param(start_utc), self._timestamp_param(end_utc)),
            )
            stale = [r["event_id"] for r in rows if r["event_id"] not in keep]
            for event_id in stale:
                self._write(f"DELETE FROM {TABLE} WHERE event_id = ?", (event_id,))
        return len(stale)

    def cleanup_old_events(self, cutoff_date: str) -> int:
        """Delete events dated strictly before cutoff_date (YYYY-MM-DD). Returns the count."""
        with self._transaction():
            return self._write(f"DELETE FROM {TABLE} WHERE date < ?", (self._date_param(cutoff_date),))

    # -- classifications --------------------------------------------------------
    # One row per event, removed automatically with the event (ON DELETE CASCADE).

    def _classifications(self, event_ids: list[str]) -> dict[str, Classification]:
        found: dict[str, Classification] = {}
        for start in range(0, len(event_ids), _CHUNK):
            chunk = event_ids[start:start + _CHUNK]
            marks = ", ".join("?" for _ in chunk)
            for row in self._query(f"{_C_SELECT} WHERE event_id IN ({marks})", chunk):
                item = self._decode_classification(row)
                found[item.event_id] = item
        return found

    def upsert_classifications(self, classifications: Iterable[Classification], now: str | None = None) -> dict[str, int]:
        """Store classifications: one row per event, rewritten only when the result changed."""
        items = list(classifications)
        now = now or utc_now()
        counts = {"inserted": 0, "updated": 0, "unchanged": 0}
        with self._transaction():
            existing = self._classifications([c.event_id for c in items])
            for item in items:
                new = Classification(**item.to_dict())
                old = existing.get(new.event_id)
                if old is None:
                    status = "inserted"
                elif any(getattr(new, c) != getattr(old, c) for c in CLASSIFICATION_CONTENT):
                    status = "updated"
                else:
                    status = "unchanged"
                new.classified_at = now
                new.updated_at = old.updated_at if status == "unchanged" else now
                values = self._encode_classification(new)
                self._write(_C_UPSERT, [values[c] for c in CLASSIFICATION_COLUMNS])
                existing[new.event_id] = new
                counts[status] += 1
        return counts

    def get_classifications(self, event_ids: Iterable[str]) -> dict[str, Classification]:
        try:
            found = self._classifications(list(event_ids))
            self._commit()
            return found
        except self._driver_errors as exc:
            self._safe_rollback()
            raise self._translate(exc) from exc

    def get_classification(self, event_id: str) -> Classification | None:
        return self.get_classifications([event_id]).get(event_id)

    def count_classifications(self) -> int:
        return self._read(f"SELECT COUNT(*) AS n FROM {CLASSIFICATION_TABLE}")[0]["n"]

    # -- actual results ---------------------------------------------------------
    # One enrichment record per event, removed automatically with the event.

    def get_actual_records(self, event_ids: Iterable[str]) -> dict[str, ActualRecord]:
        ids, found = list(event_ids), {}
        try:
            for start in range(0, len(ids), _CHUNK):
                chunk = ids[start:start + _CHUNK]
                marks = ", ".join("?" for _ in chunk)
                for row in self._query(f"{_A_SELECT} WHERE event_id IN ({marks})", chunk):
                    record = self._decode_actual(row)
                    found[record.event_id] = record
            self._commit()
            return found
        except self._driver_errors as exc:
            self._safe_rollback()
            raise self._translate(exc) from exc

    def save_actual_results(self, results: Iterable[tuple[ActualRecord, str | None]], now: str | None = None) -> int:
        """Store enrichment records, and the Actual value where one is given, as one transaction.

        Each item is (record, actual). `actual` is written to events.actual
        when it is not None; the event's identity, forecast and previous are
        never touched.
        """
        now = now or utc_now()
        written = 0
        with self._transaction():
            for record, actual in results:
                values = self._encode_actual(record)
                self._write(_A_UPSERT, [values[c] for c in ACTUALS_COLUMNS])
                if actual is not None:
                    self._write(f"UPDATE {TABLE} SET actual = ?, updated_at = ? WHERE event_id = ?",
                                (actual, self._timestamp_param(now), record.event_id))
                written += 1
        return written

    def count_actual_records(self) -> int:
        return self._read(f"SELECT COUNT(*) AS n FROM {ACTUALS_TABLE}")[0]["n"]

    # -- message deliveries -----------------------------------------------------
    # One row per (message_key, provider, destination_id): the basis for never sending twice.

    def get_delivery(self, message_key: str, provider: str, destination_id: str) -> DeliveryRecord | None:
        rows = self._read(
            f"{_D_SELECT} WHERE message_key = ? AND provider = ? AND destination_id = ?",
            (message_key, provider, destination_id))
        return self._decode_delivery(rows[0]) if rows else None

    def save_delivery(self, record: DeliveryRecord) -> None:
        """Insert the delivery, or update a previous failed attempt. A SENT row is never overwritten."""
        values = self._encode_delivery(record)
        with self._transaction():
            self._write(_D_UPSERT, [values[c] for c in DELIVERY_COLUMNS])

    def list_deliveries(self, limit: int = 50) -> list[DeliveryRecord]:
        rows = self._read(f"{_D_SELECT} ORDER BY updated_at DESC, message_key LIMIT {int(limit)}")
        return [self._decode_delivery(r) for r in rows]

    def cleanup_old_deliveries(self, cutoff: str, *, only_type: str | None = None, except_type: str | None = None) -> int:
        """Delete delivery records last touched before `cutoff` (ISO UTC instant). Returns the count."""
        sql, params = f"DELETE FROM {DELIVERIES_TABLE} WHERE updated_at < ?", [self._timestamp_param(cutoff)]
        if only_type is not None:
            sql, params = sql + " AND message_type = ?", params + [only_type]
        if except_type is not None:
            sql, params = sql + " AND message_type <> ?", params + [except_type]
        with self._transaction():
            return self._write(sql, params)

    def count_deliveries(self) -> int:
        return self._read(f"SELECT COUNT(*) AS n FROM {DELIVERIES_TABLE}")[0]["n"]

    # -- price snapshots --------------------------------------------------------
    # The price shown in each market-pulse post, keyed by its half-hour slot
    # (an ISO UTC instant kept as text, so it sorts and compares as written).

    def save_price_snapshot(self, symbol: str, slot: str, price: float, source: str,
                            source_updated_at: str | None, recorded_at: str) -> None:
        """Keep the price posted for a slot. The first price stored for a slot stays."""
        with self._transaction():
            self._write(
                f"INSERT INTO {PRICES_TABLE} ({', '.join(PRICE_COLUMNS)}) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (symbol, slot) DO NOTHING",
                (symbol, slot, float(price), source, source_updated_at, recorded_at))

    def latest_price_snapshot(self, symbol: str, before_slot: str) -> dict | None:
        """The most recent stored price of an earlier slot, or None."""
        rows = self._read(
            f"SELECT {', '.join(PRICE_COLUMNS)} FROM {PRICES_TABLE} WHERE symbol = ? AND slot < ? "
            "ORDER BY slot DESC LIMIT 1", (symbol, before_slot))
        if not rows:
            return None
        row = dict(rows[0])
        row["price"] = float(row["price"])
        return row

    def delete_price_snapshots_before(self, symbol: str, slot: str) -> int:
        with self._transaction():
            return self._write(f"DELETE FROM {PRICES_TABLE} WHERE symbol = ? AND slot < ?", (symbol, slot))

    def count_price_snapshots(self) -> int:
        return self._read(f"SELECT COUNT(*) AS n FROM {PRICES_TABLE}")[0]["n"]

    # -- reads ------------------------------------------------------------------

    def get_event(self, event_id: str) -> Event | None:
        rows = self._read(f"{_SELECT} WHERE event_id = ?", (event_id,))
        return self._decode(rows[0]) if rows else None

    def count(self) -> int:
        return self._read(f"SELECT COUNT(*) AS n FROM {TABLE}")[0]["n"]

    def query_events(
        self,
        *,
        currency: str | None = None,
        impacts: Iterable[str] | None = None,
        gold_relevance: bool | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[Event]:
        """Events matching every given condition, in chronological order."""
        where, params = [], []
        if currency is not None:
            where.append("currency = ?")
            params.append(currency)
        if impacts is not None:
            impacts = list(impacts)
            where.append(f"impact IN ({', '.join('?' for _ in impacts)})")
            params.extend(impacts)
        if gold_relevance is not None:
            where.append("gold_relevance = ?")
            params.append(self._bool_param(gold_relevance))
        if date_from is not None:
            where.append("date >= ?")
            params.append(self._date_param(date_from))
        if date_to is not None:
            where.append("date <= ?")
            params.append(self._date_param(date_to))
        sql = _SELECT
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY date, time, event_name"
        return [self._decode(r) for r in self._read(sql, params)]

    def _bool_param(self, value: bool) -> Any:
        return value
