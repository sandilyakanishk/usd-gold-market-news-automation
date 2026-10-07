"""One-off copy of the local SQLite events into PostgreSQL."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..config import Settings
from .base import DatabaseError, EventRepository
from .database import SQLiteRepository
from .postgres import PostgresRepository

log = logging.getLogger(__name__)


@dataclass
class MigrationResult:
    source_events: int
    copied: int
    already_present: int
    target_events: int


def copy_events(source: EventRepository, target: EventRepository) -> MigrationResult:
    """Copy every event from source to target without touching rows the target already has.

    Existing target rows win, because the target may hold newer data than the
    local file. That also makes a second run a no-op.
    """
    events = source.query_events()
    counts = target.import_events(events)

    missing = [e.event_id for e in events if target.get_event(e.event_id) is None]
    if missing:
        raise DatabaseError(f"Migration incomplete: {len(missing)} event(s) did not reach the target database.")

    result = MigrationResult(len(events), counts["copied"], counts["already_present"], target.count())
    log.info("Migration %s -> %s: %s", source.describe(), target.describe(), result)
    return result


def migrate_sqlite_to_postgres(settings: Settings) -> MigrationResult:
    """Copy settings.database_path (SQLite) into settings.database_url (PostgreSQL)."""
    if not settings.database_path.is_file():
        raise DatabaseError(f"No SQLite database found at {settings.database_path}; nothing to migrate.")
    with PostgresRepository(settings.database_url, connect_timeout=settings.db_connect_timeout_seconds) as target:
        target.init_schema()
        with SQLiteRepository(settings.database_path) as source:
            return copy_events(source, target)
