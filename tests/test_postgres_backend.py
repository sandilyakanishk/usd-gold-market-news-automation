"""PostgreSQL backend, configuration and backend selection -- no server needed.

The driver connection is replaced by a fake that records what is sent.
"""

from datetime import date, datetime, time, timezone
from dataclasses import replace

from urllib.parse import quote

import psycopg
import pytest

from src.config import Settings
from src.database.base import (
    DatabaseConfigError, DatabaseConnectionError, DatabaseError, SchemaMissingError,
)
from src.database.database import SQLiteRepository
from src.database.factory import open_database
from src.database.postgres import PostgresRepository, safe_target, validate_database_url

SECRET = "s3cr3t-P4ss"
URL = f"postgresql://postgres.abcdefgh:{SECRET}@aws-0-ap-south-1.pooler.supabase.com:6543/postgres"


class FakeCursor:
    def __init__(self, rows=(), rowcount=1):
        self._rows, self.rowcount = list(rows), rowcount

    def fetchall(self):
        return self._rows


class FakeConnection:
    def __init__(self, rows=(), error=None):
        self.rows, self.error = rows, error
        self.executed, self.commits, self.rollbacks, self.closed = [], 0, 0, False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if self.error:
            raise self.error
        return FakeCursor(self.rows)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def make_repo(conn=None, **kwargs):
    conn = conn or FakeConnection()
    calls = []

    def connect(url, **options):
        calls.append((url, options))
        return conn

    return PostgresRepository(URL, connect=connect, **kwargs), conn, calls


# -- initialization -------------------------------------------------------------

def test_connects_with_timeout_and_without_prepared_statements():
    repo, _, calls = make_repo(connect_timeout=7)
    (url, options), = calls
    assert url == URL
    assert options["connect_timeout"] == 7
    assert options["prepare_threshold"] is None  # required for transaction-mode poolers
    assert repo.backend == "postgres"


def test_init_schema_creates_table_constraints_and_indexes():
    repo, conn, _ = make_repo()
    repo.init_schema()
    (sql, params), = conn.executed
    assert params is None
    for fragment in (
        "CREATE TABLE IF NOT EXISTS events", "event_id        TEXT        PRIMARY KEY",
        "date            DATE", "time            TIME", "datetime_utc    TIMESTAMPTZ",
        "gold_relevance  BOOLEAN", "retrieved_at    TIMESTAMPTZ NOT NULL", "CHECK (impact IN",
        "CREATE INDEX IF NOT EXISTS idx_events_date_currency ON events (date, currency)",
        "ENABLE ROW LEVEL SECURITY",
    ):
        assert fragment in sql
    assert conn.commits == 1


def test_missing_table_is_reported_with_the_fix():
    repo, _, _ = make_repo(FakeConnection(rows=[{"oid": None}]))
    with pytest.raises(SchemaMissingError, match="--init-db"):
        repo.ensure_schema()
    present, _, _ = make_repo(FakeConnection(rows=[{"oid": "events"}]))
    present.ensure_schema()


def test_undefined_table_during_a_query_is_reported_as_schema_missing():
    repo, conn, _ = make_repo(FakeConnection(error=psycopg.errors.UndefinedTable('relation "events" does not exist')))
    with pytest.raises(SchemaMissingError, match="--init-db"):
        repo.count()
    assert conn.rollbacks == 1


# -- value conversion -----------------------------------------------------------

def test_events_are_written_with_native_types(parse, feed_text):
    (event,) = [e for e in parse(feed_text).events if e.event_name == "Unemployment Claims"]
    repo, conn, _ = make_repo(FakeConnection(rows=[]))
    repo.upsert_event(event, now="2026-10-07T19:00:00Z")
    sql, params = conn.executed[-1]
    assert "ON CONFLICT (event_id) DO UPDATE" in sql and "?" not in sql and sql.count("%s") == 17
    values = dict(zip(
        ["event_id", "date", "time", "timezone", "datetime_utc", "currency", "event_name", "impact",
         "original_impact", "gold_relevance", "forecast", "previous", "actual", "source", "source_url",
         "retrieved_at", "updated_at"], params))
    assert values["date"] == date(2026, 10, 8)
    assert values["time"] == time(18, 0)
    assert values["datetime_utc"] == datetime(2026, 10, 8, 12, 30, tzinfo=timezone.utc)
    assert values["updated_at"] == datetime(2026, 10, 7, 19, 0, tzinfo=timezone.utc)
    assert values["gold_relevance"] is True
    assert values["actual"] is None and values["impact"] == "Medium"
    assert conn.commits == 1


def test_rows_are_read_back_as_the_same_event(parse, feed_text):
    (event,) = [e for e in parse(feed_text).events if e.event_name == "Unemployment Claims"]
    repo, _, _ = make_repo()
    stored = replace(event, updated_at="2026-10-07T19:00:00Z")
    assert repo._decode(repo._encode(stored)) == stored
    # A timestamp the server returns in another zone still means the same instant.
    row = repo._encode(stored)
    row["datetime_utc"] = row["datetime_utc"].astimezone(timezone.utc).astimezone()
    assert repo._decode(row).datetime_utc == "2026-10-08T12:30:00Z"


def test_malformed_event_is_rejected_and_rolled_back(parse, feed_text):
    event = replace(parse(feed_text).events[0], date="not-a-date")
    repo, conn, _ = make_repo(FakeConnection(rows=[]))
    with pytest.raises(DatabaseError, match="Malformed event record"):
        repo.upsert_event(event)
    assert conn.rollbacks == 1 and conn.commits == 0


# -- failures -------------------------------------------------------------------

def failing_connect(message):
    def connect(url, **options):
        raise psycopg.OperationalError(message)
    return connect


@pytest.mark.parametrize("message, hint", [
    ('connection failed: FATAL:  password authentication failed for user "postgres"', "password"),
    ("connection timeout expired", "did not answer in time"),
    ("failed to resolve host 'db.example.supabase.co': [Errno 11001] getaddrinfo failed", "could not be resolved"),
    ("connection refused", "Check that the database is running"),
])
def test_connection_failures_are_clear_and_never_leak_the_password(message, hint):
    with pytest.raises(DatabaseConnectionError) as error:
        PostgresRepository(URL, connect=failing_connect(message))
    text = str(error.value)
    assert hint in text
    assert "aws-0-ap-south-1.pooler.supabase.com" in text
    assert SECRET not in text and URL not in text


@pytest.mark.parametrize("url", [None, "", "   "])
def test_missing_database_url(url):
    with pytest.raises(DatabaseConfigError, match="DATABASE_URL is not set"):
        PostgresRepository(url)


@pytest.mark.parametrize("url", [
    f"mysql://user:{SECRET}@host/db",
    f"user:{SECRET}@host:5432/postgres",
    f"postgresql://user:{SECRET}@host:notaport/postgres",
    f"postgresql://user:{SECRET}@/postgres",
    "postgresql://postgres:[YOUR-PASSWORD]@db.abcdefgh.supabase.co:5432/postgres",
])
def test_invalid_database_url_is_rejected_without_echoing_it(url):
    with pytest.raises(DatabaseConfigError) as error:
        validate_database_url(url)
    assert SECRET not in str(error.value)


def test_log_safe_description_has_no_credentials():
    repo, _, _ = make_repo()
    assert repo.describe() == "postgres (host=aws-0-ap-south-1.pooler.supabase.com port=6543 dbname=postgres)"
    assert SECRET not in safe_target(URL) and "abcdefgh" not in safe_target(URL)


# -- backend selection ----------------------------------------------------------

def test_sqlite_is_the_default_backend(settings):
    assert settings.database_backend == "sqlite"
    with open_database(settings) as db:
        assert isinstance(db, SQLiteRepository)
        assert db.count() == 0
    assert settings.database_path.is_file()


def test_sqlite_backend_creates_schema_and_reopens(settings, parse, feed_text):
    with open_database(settings) as db:
        db.upsert_events(parse(feed_text).events)
    with open_database(settings) as db:
        assert db.count() == 83
        assert db.describe() == f"sqlite ({settings.database_path})"


def test_unusable_sqlite_file_is_a_clear_error(settings):
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    settings.database_path.write_bytes(b"this is not a database file, " * 50)
    with pytest.raises(DatabaseConnectionError, match="SQLite"):
        open_database(settings)


def test_postgres_backend_is_selected_by_configuration(settings, monkeypatch):
    opened = []

    class StubRepository:
        def __init__(self, url, connect_timeout):
            opened.append((url, connect_timeout))

        def ensure_schema(self):
            opened.append("schema checked")

        def describe(self):
            return "postgres (stub)"

    monkeypatch.setattr("src.database.postgres.PostgresRepository", StubRepository)
    db = open_database(replace(settings, database_backend="postgres", database_url=URL))
    assert isinstance(db, StubRepository)
    assert opened == [(URL, 10), "schema checked"]


def test_postgres_without_url_fails_and_does_not_fall_back_to_sqlite(settings):
    with pytest.raises(DatabaseConfigError, match="DATABASE_URL is not set"):
        open_database(replace(settings, database_backend="postgres"))
    assert not settings.database_path.exists()


def test_unreachable_postgres_fails_and_does_not_fall_back_to_sqlite(settings, monkeypatch, caplog):
    monkeypatch.setattr(psycopg, "connect", failing_connect("connection timeout expired"))
    with caplog.at_level("DEBUG"):
        with pytest.raises(DatabaseConnectionError):
            open_database(replace(settings, database_backend="postgres", database_url=URL))
    assert not settings.database_path.exists()
    assert SECRET not in caplog.text


def test_unknown_backend_is_rejected(settings):
    with pytest.raises(DatabaseConfigError, match="Unknown DATABASE_BACKEND 'mongodb'"):
        open_database(replace(settings, database_backend="mongodb"))


# -- environment ----------------------------------------------------------------

@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr("src.config.load_dotenv", lambda path: None)
    for name in ("DATABASE_BACKEND", "DATABASE_URL", "SQLITE_PATH", "DATABASE_PATH",
                 "CALENDAR_RETENTION_DAYS", "DB_CONNECT_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_environment_defaults(env):
    settings = Settings.from_env()
    assert (settings.database_backend, settings.database_url) == ("sqlite", None)
    assert settings.database_path.name == "events.db"
    assert settings.calendar_retention_days == 14


def test_environment_selects_postgres(env):
    env.setenv("DATABASE_BACKEND", "Postgres")
    env.setenv("DATABASE_URL", URL)
    env.setenv("SQLITE_PATH", "data/local.sqlite")
    env.setenv("CALENDAR_RETENTION_DAYS", "30")
    settings = Settings.from_env()
    assert (settings.database_backend, settings.database_url) == ("postgres", URL)
    assert settings.database_path.name == "local.sqlite"
    assert settings.calendar_retention_days == 30


def test_database_url_alone_does_not_switch_the_backend(env):
    env.setenv("DATABASE_URL", URL)
    assert Settings.from_env().database_backend == "sqlite"


@pytest.mark.parametrize("value", ["0", "-3", "two weeks"])
def test_invalid_retention_is_rejected(env, value):
    env.setenv("CALENDAR_RETENTION_DAYS", value)
    with pytest.raises(ValueError, match="CALENDAR_RETENTION_DAYS"):
        Settings.from_env()


# -- passwords with special characters ------------------------------------------

AWKWARD = "p@ss/w:rd#19?x"
HOST = "aws-0-ap-south-1.pooler.supabase.com"


def test_unencoded_special_characters_in_the_password_are_handled():
    calls = []
    PostgresRepository(f"postgresql://postgres.ref:{AWKWARD}@{HOST}:5432/postgres",
                       connect=lambda url, **options: calls.append(url) or FakeConnection())
    (sent,) = calls
    assert sent == f"postgresql://postgres.ref:p%40ss%2Fw%3Ard%2319%3Fx@{HOST}:5432/postgres"
    assert psycopg.conninfo.conninfo_to_dict(sent)["password"] == AWKWARD
    assert psycopg.conninfo.conninfo_to_dict(sent)["host"] == HOST


def test_an_already_encoded_password_is_left_alone():
    url = f"postgresql://postgres.ref:p%40ss-word_1@{HOST}:5432/postgres"
    assert validate_database_url(url) == url


def test_target_description_is_correct_with_an_awkward_password():
    repo = PostgresRepository(f"postgresql://postgres.ref:{AWKWARD}@{HOST}:5432/postgres",
                              connect=lambda url, **options: FakeConnection())
    assert repo.describe() == f"postgres (host={HOST} port=5432 dbname=postgres)"


@pytest.mark.parametrize("password", [AWKWARD, "plain-Secret99"])
def test_driver_messages_are_scrubbed_of_the_password(password):
    def connect(url, **options):
        raise psycopg.OperationalError(f"failed to resolve host for {url} (password {password})")

    with pytest.raises(DatabaseConnectionError) as error:
        PostgresRepository(f"postgresql://postgres.ref:{password}@{HOST}:5432/postgres", connect=connect)
    text = str(error.value)
    assert password not in text and quote(password, safe="") not in text
    assert "***" in text
