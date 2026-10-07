"""Chooses the storage backend from configuration."""

from __future__ import annotations

import logging

from ..config import Settings
from .base import DatabaseConfigError, EventRepository
from .database import SQLiteRepository

log = logging.getLogger(__name__)

BACKENDS = ("sqlite", "postgres")


def open_database(settings: Settings, *, require_schema: bool = True) -> EventRepository:
    """Open the configured database.

    There is no fallback: if postgres is selected and unavailable this raises,
    it never quietly writes to a local SQLite file instead.
    """
    backend = (settings.database_backend or "sqlite").strip().lower()
    if backend == "sqlite":
        db: EventRepository = SQLiteRepository(settings.database_path)
    elif backend in ("postgres", "postgresql"):
        from .postgres import PostgresRepository

        repo = PostgresRepository(settings.database_url, connect_timeout=settings.db_connect_timeout_seconds)
        if require_schema:
            try:
                repo.ensure_schema()
            except Exception:
                repo.close()
                raise
        db = repo
    else:
        raise DatabaseConfigError(
            f"Unknown DATABASE_BACKEND '{settings.database_backend}'. Use one of: {', '.join(BACKENDS)}.")
    log.info("Database backend: %s", db.describe())
    return db
