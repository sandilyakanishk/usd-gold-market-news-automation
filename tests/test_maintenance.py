"""Retention cleanup, migration and the database maintenance commands."""

from dataclasses import replace
from datetime import date

import pytest

from src import main as cli
from src.collector import forex_factory
from src.database import migrate
from src.database.base import DatabaseError
from src.database.database import SQLiteRepository
from src.pipeline import cleanup_old_events

from .test_postgres_backend import URL, SECRET
from .test_retrieval import fake_urlopen

TODAY = date(2026, 10, 8)


def dated(event, day, suffix):
    return replace(event, event_id=f"ff-old-{suffix}", date=day, datetime_utc=f"{day}T12:30:00Z")


@pytest.fixture
def history(db, parse, feed_text):
    """This week's USD events plus three older ones around the 14-day boundary."""
    events = [e for e in parse(feed_text).events if e.currency == "USD"]
    db.upsert_events(events)
    db.upsert_events([
        dated(events[0], "2026-09-01", "a"),  # long gone
        dated(events[0], "2026-09-23", "b"),  # 15 days before TODAY
        dated(events[0], "2026-09-24", "c"),  # exactly 14 days before TODAY
    ])
    return db


# -- retention ------------------------------------------------------------------

def test_default_retention_keeps_fourteen_days(history, settings):
    assert settings.calendar_retention_days == 14
    assert cleanup_old_events(settings, history, TODAY) == 2
    assert history.get_event("ff-old-a") is None and history.get_event("ff-old-b") is None
    assert history.get_event("ff-old-c") is not None
    assert history.count() == 24


def test_yesterday_is_never_deleted(history, settings):
    cleanup_old_events(replace(settings, calendar_retention_days=1), history, TODAY)
    dates = {e.date for e in history.query_events()}
    assert "2026-10-07" in dates and "2026-10-06" not in dates


def test_retention_period_is_configurable(history, settings):
    assert cleanup_old_events(replace(settings, calendar_retention_days=60), history, TODAY) == 0
    assert cleanup_old_events(replace(settings, calendar_retention_days=30), history, TODAY) == 1
    assert history.count() == 25


def test_cleanup_is_repeatable(history, settings):
    assert cleanup_old_events(settings, history, TODAY) == 2
    assert cleanup_old_events(settings, history, TODAY) == 0


def test_cleanup_uses_today_in_the_application_timezone(history, settings, monkeypatch):
    from src import pipeline

    class Clock(pipeline.datetime):
        @classmethod
        def now(cls, tz=None):
            # 20:00 UTC on the 7th is already the 8th in India.
            return cls(2026, 10, 7, 20, 0, tzinfo=pipeline.timezone.utc).astimezone(tz)

    monkeypatch.setattr(pipeline, "datetime", Clock)
    assert cleanup_old_events(settings, history) == 2  # cutoff 2026-09-24, as for TODAY


# -- migration ------------------------------------------------------------------

@pytest.fixture
def local_sqlite(settings, parse, feed_text):
    with SQLiteRepository(settings.database_path) as source:
        source.upsert_events((e for e in parse(feed_text).events if e.currency == "USD"),
                             now="2026-10-07T19:00:00Z")
    return settings


@pytest.fixture
def fake_postgres(monkeypatch, tmp_path):
    """Stand-in for the PostgreSQL target: a second SQLite file behind the same interface."""
    target_path = tmp_path / "target.db"
    opened = []

    class StandIn(SQLiteRepository):
        def __init__(self, url, connect_timeout=10):
            opened.append(url)
            super().__init__(target_path)

    monkeypatch.setattr(migrate, "PostgresRepository", StandIn)
    return target_path, opened


def test_migration_copies_everything_exactly(local_sqlite, fake_postgres):
    target_path, opened = fake_postgres
    settings = replace(local_sqlite, database_url=URL)
    result = migrate.migrate_sqlite_to_postgres(settings)
    assert (result.source_events, result.copied, result.already_present, result.target_events) == (23, 23, 0, 23)
    assert opened == [URL]
    with SQLiteRepository(settings.database_path) as source, SQLiteRepository(target_path) as target:
        assert target.query_events() == source.query_events()
        assert all(e.actual is None for e in target.query_events())
        assert sum(e.gold_relevance for e in target.query_events()) == 13
        assert [e.impact for e in target.query_events()] == [e.impact for e in source.query_events()]


def test_migration_can_be_run_again_safely(local_sqlite, fake_postgres):
    target_path, _ = fake_postgres
    settings = replace(local_sqlite, database_url=URL)
    migrate.migrate_sqlite_to_postgres(settings)
    with SQLiteRepository(target_path) as target:
        event_id = target.query_events()[0].event_id
        target.set_actual(event_id, "1.0%")  # production moved on after the first run
    result = migrate.migrate_sqlite_to_postgres(settings)
    assert (result.copied, result.already_present, result.target_events) == (0, 23, 23)
    with SQLiteRepository(target_path) as target:
        assert target.get_event(event_id).actual == "1.0%"


def test_migration_without_a_local_database_is_an_error(settings, fake_postgres):
    with pytest.raises(DatabaseError, match="nothing to migrate"):
        migrate.migrate_sqlite_to_postgres(replace(settings, database_url=URL))
    assert not settings.database_path.exists()


def test_migration_requires_database_url(local_sqlite):
    with pytest.raises(DatabaseError, match="DATABASE_URL is not set"):
        migrate.migrate_sqlite_to_postgres(local_sqlite)


# -- command line ---------------------------------------------------------------

@pytest.fixture
def run(monkeypatch, settings, feed_text, capsys):
    state = {"settings": settings}
    monkeypatch.setattr(cli.Settings, "from_env", classmethod(lambda cls: state["settings"]))
    monkeypatch.setattr(forex_factory.urllib.request, "urlopen", fake_urlopen(feed_text))

    class FixedDateTime(cli.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 8, 12, 0, tzinfo=tz)

    monkeypatch.setattr(cli, "datetime", FixedDateTime)

    def _run(*argv, **overrides):
        state["settings"] = replace(settings, **overrides)
        code = cli.main(list(argv))
        return code, capsys.readouterr()
    return _run


def add_old_events(settings, parse, feed_text):
    event = parse(feed_text).events[0]
    with SQLiteRepository(settings.database_path) as db:
        db.upsert_events([dated(event, "2026-09-01", "a"), dated(event, "2026-09-24", "c")])


def test_cleanup_command(run, settings, parse, feed_text):
    add_old_events(settings, parse, feed_text)
    code, out = run("--cleanup")
    assert code == 0
    assert out.out.strip() == "Removed 1 event(s) dated before 2026-09-24 (retention: 14 days)."
    with SQLiteRepository(settings.database_path) as db:
        assert db.get_event("ff-old-a") is None and db.get_event("ff-old-c") is not None


def test_a_normal_run_cleans_up_automatically(run, settings, parse, feed_text):
    add_old_events(settings, parse, feed_text)
    code, out = run("--today")
    assert code == 0 and "7 event(s), 0 high impact" in out.out
    with SQLiteRepository(settings.database_path) as db:
        assert db.get_event("ff-old-a") is None
        assert db.get_event("ff-old-c") is not None
        assert db.count() == 24


def test_no_fetch_run_does_not_delete_anything(run, settings, parse, feed_text):
    add_old_events(settings, parse, feed_text)
    run("--all", "--no-fetch")
    with SQLiteRepository(settings.database_path) as db:
        assert db.get_event("ff-old-a") is not None


def test_init_db_command(run, settings):
    code, out = run("--init-db")
    assert code == 0
    assert out.out.strip() == f"Database ready: sqlite ({settings.database_path})"


def test_postgres_selected_without_url_fails_loudly(run, settings):
    code, out = run("--today", database_backend="postgres")
    assert code == 1
    assert out.err.startswith("DATABASE ERROR: DATABASE_URL is not set")
    assert out.out == ""
    assert not settings.database_path.exists()  # nothing was written locally instead


def test_unreachable_postgres_fails_loudly_without_leaking_secrets(run, settings, monkeypatch, caplog):
    import psycopg

    def refuse(url, **options):
        raise psycopg.OperationalError('connection failed: FATAL:  password authentication failed for user "postgres"')

    monkeypatch.setattr(psycopg, "connect", refuse)
    with caplog.at_level("DEBUG"):
        code, out = run("--week", database_backend="postgres", database_url=URL)
    assert code == 1
    assert "DATABASE ERROR: Could not connect to PostgreSQL" in out.err
    assert SECRET not in out.err and SECRET not in out.out
    assert "Could not connect to PostgreSQL" in caplog.text and SECRET not in caplog.text
    assert not settings.database_path.exists()


def test_migrate_command_reports_counts(run, local_sqlite, fake_postgres):
    code, out = run("--migrate-to-postgres", database_url=URL)
    assert code == 0
    assert out.out.strip() == ("Migration complete: 23 event(s) in SQLite, 23 copied, "
                               "0 already in PostgreSQL. PostgreSQL now holds 23.")
    code, out = run("--migrate-to-postgres", database_url=URL)
    assert "0 copied, 23 already in PostgreSQL" in out.out


def test_migrate_command_without_url_fails(run, local_sqlite):
    code, out = run("--migrate-to-postgres")
    assert code == 1 and "DATABASE_URL is not set" in out.err
