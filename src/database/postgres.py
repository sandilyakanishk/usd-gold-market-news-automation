"""PostgreSQL backend: production storage (Supabase or any other Postgres).

Uses plain PostgreSQL through psycopg; nothing here is Supabase-specific.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote, unquote, urlsplit

from ..actuals.models import ActualRecord
from ..classification.models import Classification
from ..collector.models import Event
from ..delivery.models import DeliveryRecord
from .base import (
    ACTUALS_TABLE,
    DELIVERIES_TABLE,
    CLASSIFICATION_TABLE,
    TABLE,
    DatabaseConfigError,
    DatabaseConnectionError,
    DatabaseError,
    EventRepository,
    SchemaMissingError,
)

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("postgres_schema.sql")
_SCHEMES = ("postgresql://", "postgres://")
_TIMESTAMP_COLUMNS = ("datetime_utc", "retrieved_at", "updated_at")
_URL_SAFE_PASSWORD = re.compile(r"(?:[A-Za-z0-9._~\-]|%[0-9A-Fa-f]{2})*")


def _split_credentials(url: str) -> tuple[str, str, str | None, str]:
    """Split into (scheme, user, password, rest), taking the LAST '@' as the end of the credentials.

    A password may itself contain '@', '/', '?' or '#'; a host, port and
    database name cannot contain '@'.
    """
    scheme, _, remainder = url.partition("://")
    userinfo, at, rest = remainder.rpartition("@")
    if not at:
        return scheme, "", None, remainder
    user, colon, password = userinfo.partition(":")
    return scheme, user, (password if colon else None), rest


def _encode_password(url: str) -> str:
    """Percent-encode a password that was pasted in as-is.

    Without this, a password containing '@' makes the driver read part of
    the password as the host name.
    """
    scheme, user, password, rest = _split_credentials(url)
    if password is None or _URL_SAFE_PASSWORD.fullmatch(password):
        return url
    return f"{scheme}://{user}:{quote(password, safe='')}@{rest}"


def validate_database_url(url: str | None) -> str:
    """Return the URL if it is usable, else raise DatabaseConfigError (never echoing it)."""
    if not url or not url.strip():
        raise DatabaseConfigError(
            "DATABASE_URL is not set, and the PostgreSQL database needs it. Set it in the environment "
            "or in .env (for Supabase: the connection string from the project's Connect panel).")
    url = url.strip()
    if not url.startswith(_SCHEMES):
        raise DatabaseConfigError(
            "DATABASE_URL is not a PostgreSQL connection string. "
            "Expected the form postgresql://USER:PASSWORD@HOST:PORT/DBNAME")
    if "YOUR-PASSWORD" in url:
        raise DatabaseConfigError(
            "DATABASE_URL still contains the [YOUR-PASSWORD] placeholder; replace it, square brackets "
            "included, with the database password.")
    url = _encode_password(url)
    try:
        parts = urlsplit(url)
        parts.port  # raises ValueError on a non-numeric port
    except ValueError as exc:
        raise DatabaseConfigError("DATABASE_URL could not be parsed; check the host and port.") from exc
    if not parts.hostname:
        raise DatabaseConfigError("DATABASE_URL has no host name.")
    return url


def safe_target(url: str) -> str:
    """Host, port and database name only -- suitable for logs."""
    parts = urlsplit(url)
    return f"host={parts.hostname} port={parts.port or 5432} dbname={parts.path.lstrip('/') or 'postgres'}"


def _parse_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _format_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class PostgresRepository(EventRepository):
    backend = "postgres"

    def __init__(self, database_url: str | None, *, connect_timeout: int = 10,
                 connect: Callable[..., Any] | None = None):
        """`connect` replaces psycopg.connect in tests."""
        url = validate_database_url(database_url)
        self._target = safe_target(url)
        password = _split_credentials(url)[2] or ""
        # Never let the password through in a driver message, encoded or not.
        self._secrets = sorted({s for s in (password, unquote(password)) if len(s) >= 3}, key=len, reverse=True)
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise DatabaseConfigError(
                "The PostgreSQL backend needs the 'psycopg' package: pip install -r requirements.txt") from exc
        self._driver_errors = (psycopg.Error,)
        self._psycopg = psycopg
        try:
            # prepare_threshold=None: no server-side prepared statements, so the
            # same code works through transaction-mode connection poolers.
            self.conn = (connect or psycopg.connect)(
                url, connect_timeout=connect_timeout, row_factory=dict_row, prepare_threshold=None)
        except psycopg.Error as exc:
            raise self._connection_error(exc) from exc

    def _connection_error(self, exc: BaseException) -> DatabaseConnectionError:
        detail = self._redact(str(exc)).strip()
        detail = detail.splitlines()[0] if detail else type(exc).__name__
        text = detail.lower()
        if "password authentication failed" in text or "authentication" in text:
            hint = "Check the user name and password in DATABASE_URL."
        elif "timeout" in text or "timed out" in text:
            hint = ("The server did not answer in time. Check the host and port; on a network without IPv6, "
                    "use a pooler connection string instead of the direct one.")
        elif "resolve" in text or "getaddrinfo" in text or "name or service" in text or "unknown host" in text:
            hint = "The host name could not be resolved. Check the host in DATABASE_URL and your network."
        else:
            hint = "Check that the database is running and that DATABASE_URL is correct."
        return DatabaseConnectionError(f"Could not connect to PostgreSQL ({self._target}): {detail}. {hint}")

    def _redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text

    def _translate(self, exc: BaseException) -> DatabaseError:
        if isinstance(exc, self._psycopg.errors.UndefinedTable):
            return SchemaMissingError(
                f"A required table does not exist in PostgreSQL ({self._target}). "
                "Create it with: python -m src.main --init-db")
        if isinstance(exc, self._psycopg.OperationalError):
            return DatabaseConnectionError(
                f"Lost the PostgreSQL connection ({self._target}): {self._redact(str(exc))}")
        return DatabaseError(self._redact(str(super()._translate(exc))))

    # -- hooks ------------------------------------------------------------------

    def init_schema(self) -> None:
        with self._transaction():
            self.conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))

    def ensure_schema(self) -> None:
        """Raise SchemaMissingError unless the events table exists."""
        for table in (TABLE, CLASSIFICATION_TABLE, ACTUALS_TABLE, DELIVERIES_TABLE):
            rows = self._read("SELECT to_regclass(?::text) AS oid", (table,))
            if rows[0]["oid"] is None:
                raise SchemaMissingError(
                    f"The '{table}' table does not exist in PostgreSQL ({self._target}). "
                    "Create it with: python -m src.main --init-db")

    def describe(self) -> str:
        return f"postgres ({self._target})"

    def close(self) -> None:
        self.conn.close()

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        return self.conn.execute(sql.replace("?", "%s"), tuple(params)).fetchall()

    def _write(self, sql: str, params: Iterable[Any] = ()) -> int:
        return self.conn.execute(sql.replace("?", "%s"), tuple(params)).rowcount

    def _commit(self) -> None:
        self.conn.commit()

    def _rollback(self) -> None:
        self.conn.rollback()

    def _encode(self, event: Event) -> dict:
        values = event.to_dict()
        values["date"] = date.fromisoformat(event.date) if event.date else None
        values["time"] = time.fromisoformat(event.time) if event.time else None
        for column in _TIMESTAMP_COLUMNS:
            values[column] = _parse_timestamp(values[column])
        return values

    def _decode(self, row: dict) -> Event:
        row = dict(row)
        row["date"] = row["date"].isoformat() if row["date"] else None
        row["time"] = row["time"].strftime("%H:%M") if row["time"] else None
        for column in _TIMESTAMP_COLUMNS:
            row[column] = _format_timestamp(row[column])
        return Event(**row)

    def _encode_classification(self, classification: Classification) -> dict:
        values = classification.to_dict()
        for column in ("classified_at", "updated_at"):
            values[column] = _parse_timestamp(values[column])
        return values

    def _decode_classification(self, row: dict) -> Classification:
        row = dict(row)
        for column in ("classified_at", "updated_at"):
            row[column] = _format_timestamp(row[column])
        return Classification(**row)

    def _encode_actual(self, record: ActualRecord) -> dict:
        values = record.to_dict()
        for column in ("actual_retrieved_at", "actual_updated_at", "updated_at"):
            values[column] = _parse_timestamp(values[column])
        return values

    def _decode_actual(self, row: dict) -> ActualRecord:
        row = dict(row)
        for column in ("actual_retrieved_at", "actual_updated_at", "updated_at"):
            row[column] = _format_timestamp(row[column])
        return ActualRecord(**row)

    def _encode_delivery(self, record: DeliveryRecord) -> dict:
        values = record.to_dict()
        for column in ("sent_at", "created_at", "updated_at"):
            values[column] = _parse_timestamp(values[column])
        return values

    def _decode_delivery(self, row: dict) -> DeliveryRecord:
        row = dict(row)
        for column in ("sent_at", "created_at", "updated_at"):
            row[column] = _format_timestamp(row[column])
        return DeliveryRecord(**row)

    def _date_param(self, value: str) -> date:
        return date.fromisoformat(value)

    def _timestamp_param(self, value: str) -> datetime:
        return _parse_timestamp(value)
