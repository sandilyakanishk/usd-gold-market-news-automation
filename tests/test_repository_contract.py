"""Behaviour every storage backend must share.

Runs against SQLite always. It also runs against a real PostgreSQL server when
TEST_DATABASE_URL is set (opt-in; use a direct or session-mode connection).
The PostgreSQL run works inside its own temporary schema and never touches
the real `events` table.
"""

import json
import os
import uuid
from dataclasses import replace

import pytest

from src.database.base import _INSERT, ALL_COLUMNS, DatabaseError
from src.database.database import SQLiteRepository
from src.database.migrate import copy_events
from src.filters import gold_usd_filters as f

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
NOW = "2026-10-07T19:00:00Z"


@pytest.fixture(params=["sqlite", "postgres"])
def repo(request):
    if request.param == "sqlite":
        with SQLiteRepository(":memory:") as database:
            yield database
        return
    if not TEST_DATABASE_URL:
        pytest.skip("set TEST_DATABASE_URL to run the PostgreSQL integration tests")
    from src.database.postgres import PostgresRepository

    schema = f"ff_test_{uuid.uuid4().hex[:12]}"
    database = PostgresRepository(TEST_DATABASE_URL)
    database.conn.execute(f"CREATE SCHEMA {schema}")
    database.conn.execute(f"SET search_path TO {schema}")
    database.conn.commit()
    try:
        database.init_schema()
        yield database
    finally:
        database.conn.rollback()
        database.conn.execute(f"DROP SCHEMA {schema} CASCADE")
        database.conn.commit()
        database.close()


@pytest.fixture
def usd_events(parse, feed_text):
    return [e for e in parse(feed_text).events if e.currency == "USD"]


@pytest.fixture
def loaded(repo, usd_events):
    repo.upsert_events(usd_events, now=NOW)
    return repo


def claims(repo):
    (event,) = [e for e in repo.query_events() if e.event_name == "Unemployment Claims"]
    return event


def test_schema_can_be_initialized_repeatedly(repo):
    repo.init_schema()
    repo.init_schema()
    assert repo.count() == 0


def test_insert_preserves_every_field(loaded, usd_events):
    for original in usd_events:
        stored = loaded.get_event(original.event_id)
        assert stored == replace(original, updated_at=NOW)
        assert isinstance(stored.gold_relevance, bool)


def test_dates_and_times_keep_their_meaning(loaded):
    event = claims(loaded)
    assert (event.date, event.time, event.timezone) == ("2026-10-08", "18:00", "Asia/Kolkata")
    assert event.datetime_utc == "2026-10-08T12:30:00Z"
    assert event.retrieved_at == "2026-10-07T18:30:27Z"


def test_nulls_stay_null(loaded):
    (minutes,) = [e for e in loaded.query_events() if e.event_name == "FOMC Meeting Minutes"]
    assert minutes.forecast is None and minutes.previous is None and minutes.actual is None
    assert all(e.actual is None for e in loaded.query_events())


def test_event_without_timezone_round_trips(repo, parse):
    feed = json.dumps([{"title": "CPI m/m", "country": "USD", "date": "2026-10-08T08:30:00", "impact": "High"}])
    (event,) = parse(feed).events
    repo.upsert_event(event, now=NOW)
    stored = repo.get_event(event.event_id)
    assert stored.timezone is None and stored.datetime_utc is None
    assert (stored.date, stored.time) == ("2026-10-08", "08:30")


def test_reprocessing_the_same_feed_creates_no_duplicates(loaded, usd_events, parse, feed_text):
    assert loaded.upsert_events(usd_events) == {"inserted": 0, "updated": 0, "unchanged": 23}
    again = [e for e in parse(feed_text).events if e.currency == "USD"]
    assert loaded.upsert_events(again) == {"inserted": 0, "updated": 0, "unchanged": 23}
    assert loaded.count() == 23


def test_duplicate_inside_one_batch_is_stored_once(repo, usd_events):
    counts = repo.upsert_events([usd_events[0], usd_events[0]], now=NOW)
    assert counts == {"inserted": 1, "updated": 0, "unchanged": 1}
    assert repo.count() == 1


def test_primary_key_rejects_a_plain_duplicate_insert(loaded, usd_events):
    values = loaded._encode(replace(usd_events[0], updated_at=NOW))
    with pytest.raises(DatabaseError):
        with loaded._transaction():
            loaded._write(_INSERT, [values[c] for c in ALL_COLUMNS])
    assert loaded.count() == 23  # and the connection is still usable


def test_update_changes_the_existing_row(loaded):
    before = claims(loaded)
    revised = replace(before, forecast="210K", retrieved_at="2026-10-08T01:00:00Z")
    assert loaded.upsert_event(revised, now="2026-10-08T01:00:05Z") == "updated"
    after = claims(loaded)
    assert loaded.count() == 23
    assert (after.event_id, after.forecast) == (before.event_id, "210K")
    assert (after.updated_at, after.retrieved_at) == ("2026-10-08T01:00:05Z", "2026-10-08T01:00:00Z")


def test_unchanged_event_keeps_updated_at(loaded, usd_events):
    later = [replace(e, retrieved_at="2026-10-07T20:00:00Z") for e in usd_events]
    loaded.upsert_events(later, now="2026-10-07T20:00:05Z")
    assert (claims(loaded).updated_at, claims(loaded).retrieved_at) == (NOW, "2026-10-07T20:00:00Z")


def test_actual_can_be_filled_in_and_survives_later_syncs(loaded, usd_events):
    event = claims(loaded)
    assert loaded.set_actual(event.event_id, "205K", now="2026-10-08T12:31:00Z")
    assert not loaded.set_actual("ff-does-not-exist", "1")
    loaded.upsert_events(usd_events)  # the feed still has no actual
    after = claims(loaded)
    assert (after.actual, after.updated_at) == ("205K", "2026-10-08T12:31:00Z")


def test_filters_give_the_step_1_results(loaded):
    assert len(f.get_usd_events(loaded)) == 23
    assert len(f.get_high_impact_usd_events(loaded)) == 1
    assert len(f.get_medium_impact_usd_events(loaded)) == 6
    assert len(f.get_low_impact_usd_events(loaded)) == 16
    assert len(f.get_gold_events(loaded)) == 13
    assert [e.event_name for e in f.get_high_impact_gold_events(loaded)] == ["FOMC Meeting Minutes"]
    assert len(f.get_medium_impact_gold_events(loaded)) == 5
    assert len(f.get_events_for_date(loaded, "2026-10-08")) == 7
    assert len(f.get_events_for_date_range(loaded, "2026-10-06", "2026-10-07")) == 11
    events = f.get_usd_events(loaded)
    assert events == sorted(events, key=lambda e: (e.date, e.time, e.event_name))


def test_rescheduled_event_replaces_the_old_row(loaded, parse, feed_text):
    moved = json.loads(feed_text)
    for record in moved:
        if record["title"] == "Unemployment Claims":
            record["date"] = "2026-10-09T08:30:00-04:00"
    everything = parse(json.dumps(moved)).events
    events = [e for e in everything if e.currency == "USD"]
    loaded.upsert_events(events)
    instants = [e.datetime_utc for e in everything]
    assert loaded.delete_missing((e.event_id for e in events), min(instants), max(instants)) == 1
    assert loaded.count() == 23
    assert claims(loaded).date == "2026-10-09"
    assert loaded.delete_missing([], "2026-10-11T00:00:00Z", "2026-10-17T23:59:59Z") == 0


def test_cleanup_removes_only_events_before_the_cutoff(loaded):
    # Fixture dates run from 2026-10-05 to 2026-10-10.
    assert loaded.cleanup_old_events("2026-10-05") == 0
    assert loaded.count() == 23
    removed = loaded.cleanup_old_events("2026-10-08")
    assert removed == 13
    assert min(e.date for e in loaded.query_events()) == "2026-10-08"
    assert loaded.cleanup_old_events("2026-10-08") == 0


def test_copy_between_databases_is_exact_and_repeatable(loaded):
    loaded.set_actual(claims(loaded).event_id, "205K", now="2026-10-08T12:31:00Z")
    with SQLiteRepository(":memory:") as source:
        first = copy_events(loaded, source)  # this backend -> SQLite
        assert (first.copied, first.already_present, first.target_events) == (23, 0, 23)
        # ...and back again into the already-populated backend: nothing changes.
        result = copy_events(source, loaded)
        assert (result.source_events, result.copied, result.already_present) == (23, 0, 23)
        assert source.query_events() == loaded.query_events()


def test_import_never_overwrites_rows_already_present(repo, usd_events):
    rows = [replace(e, updated_at=NOW) for e in usd_events]
    assert repo.import_events(rows) == {"copied": 23, "already_present": 0}
    repo.set_actual(usd_events[0].event_id, "9.9%", now="2026-10-09T00:00:00Z")
    assert repo.import_events(rows) == {"copied": 0, "already_present": 23}
    assert repo.get_event(usd_events[0].event_id).actual == "9.9%"
    assert repo.count() == 23
