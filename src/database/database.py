"""SQLite backend: local development and tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable

from ..classification.models import Classification
from ..collector.models import Event
from .base import DatabaseConnectionError, EventRepository, utc_now  # noqa: F401  (utc_now re-exported)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id        TEXT PRIMARY KEY,
    date            TEXT,
    time            TEXT,
    timezone        TEXT,
    datetime_utc    TEXT,
    currency        TEXT NOT NULL,
    event_name      TEXT NOT NULL,
    impact          TEXT NOT NULL,
    original_impact TEXT,
    gold_relevance  INTEGER NOT NULL DEFAULT 0,
    forecast        TEXT,
    previous        TEXT,
    actual          TEXT,
    source          TEXT NOT NULL,
    source_url      TEXT NOT NULL,
    retrieved_at    TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_date ON events (date, time);
CREATE INDEX IF NOT EXISTS idx_events_filter ON events (currency, impact, gold_relevance);

CREATE TABLE IF NOT EXISTS event_classifications (
    event_id               TEXT PRIMARY KEY REFERENCES events (event_id) ON DELETE CASCADE,
    gold_relevance         INTEGER NOT NULL,
    gold_relevance_level   TEXT NOT NULL,
    gold_relevance_reason  TEXT NOT NULL,
    category               TEXT NOT NULL,
    priority               TEXT NOT NULL,
    priority_score         INTEGER NOT NULL,
    highlight_required     INTEGER NOT NULL DEFAULT 0,
    classification_reason  TEXT NOT NULL,
    classification_version TEXT NOT NULL,
    classified_at          TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_actuals (
    event_id            TEXT PRIMARY KEY REFERENCES events (event_id) ON DELETE CASCADE,
    release_status      TEXT NOT NULL,
    status_reason       TEXT NOT NULL DEFAULT '',
    actual_source       TEXT,
    actual_source_event TEXT,
    actual_period       TEXT,
    actual_revision     INTEGER NOT NULL DEFAULT 0,
    surprise_status     TEXT NOT NULL,
    surprise_value      REAL,
    actual_retrieved_at TEXT,
    actual_updated_at   TEXT,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS message_deliveries (
    message_key         TEXT NOT NULL,
    provider            TEXT NOT NULL,
    destination_id      TEXT NOT NULL,
    destination         TEXT NOT NULL,
    message_type        TEXT NOT NULL,
    status              TEXT NOT NULL,
    provider_message_id TEXT,
    sent_at             TEXT,
    error               TEXT,
    attempts            INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    PRIMARY KEY (message_key, provider, destination_id)
);

CREATE TABLE IF NOT EXISTS price_snapshots (
    symbol            TEXT NOT NULL,
    slot              TEXT NOT NULL,
    price             REAL NOT NULL,
    source            TEXT NOT NULL,
    source_updated_at TEXT,
    recorded_at       TEXT NOT NULL,
    PRIMARY KEY (symbol, slot)
);

CREATE TABLE IF NOT EXISTS daily_prices (
    symbol  TEXT NOT NULL,
    day     TEXT NOT NULL,
    open    REAL NOT NULL,
    high    REAL NOT NULL,
    low     REAL NOT NULL,
    close   REAL NOT NULL,
    samples INTEGER NOT NULL,
    PRIMARY KEY (symbol, day)
);

CREATE TABLE IF NOT EXISTS content_state (
    kind       TEXT PRIMARY KEY,
    position   INTEGER NOT NULL DEFAULT 0,
    recent     TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);
"""


class SQLiteRepository(EventRepository):
    backend = "sqlite"
    _driver_errors = (sqlite3.Error,)

    def __init__(self, path: str | Path):
        self.path = str(path)
        try:
            if self.path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.path)
        except (sqlite3.Error, OSError) as exc:
            raise DatabaseConnectionError(f"Could not open SQLite database at {self.path}: {exc}") from exc
        self.conn.row_factory = sqlite3.Row
        # Needed for ON DELETE CASCADE from events to event_classifications.
        self.conn.execute("PRAGMA foreign_keys = ON")
        try:
            self.init_schema()
        except sqlite3.Error as exc:
            self.conn.close()
            raise DatabaseConnectionError(
                f"{self.path} could not be used as a SQLite database: {exc}") from exc

    def init_schema(self) -> None:
        self.conn.executescript(_SCHEMA)

    def describe(self) -> str:
        return f"sqlite ({self.path})"

    def close(self) -> None:
        self.conn.close()

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]

    def _write(self, sql: str, params: Iterable[Any] = ()) -> int:
        return self.conn.execute(sql, tuple(params)).rowcount

    def _commit(self) -> None:
        self.conn.commit()

    def _rollback(self) -> None:
        self.conn.rollback()

    def _encode(self, event: Event) -> dict:
        values = event.to_dict()
        values["gold_relevance"] = int(event.gold_relevance)
        return values

    def _decode(self, row: dict) -> Event:
        row = dict(row)
        row["gold_relevance"] = bool(row["gold_relevance"])
        return Event(**row)

    def _encode_classification(self, classification: Classification) -> dict:
        values = classification.to_dict()
        values["gold_relevance"] = int(classification.gold_relevance)
        values["highlight_required"] = int(classification.highlight_required)
        return values

    def _decode_classification(self, row: dict) -> Classification:
        row = dict(row)
        row["gold_relevance"] = bool(row["gold_relevance"])
        row["highlight_required"] = bool(row["highlight_required"])
        return Classification(**row)

    def _bool_param(self, value: bool) -> int:
        return int(value)


# The name the rest of the code base has always used for the local database.
Database = SQLiteRepository
