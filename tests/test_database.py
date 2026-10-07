import json
from dataclasses import replace

from src.database.database import Database


def usd(parse, feed_text):
    return [e for e in parse(feed_text).events if e.currency == "USD"]


def claims(db):
    (event,) = [e for e in db.query_events() if e.event_name == "Unemployment Claims"]
    return event


def test_all_fields_round_trip(db, parse, feed_text):
    events = usd(parse, feed_text)
    db.upsert_events(events, now="2026-10-07T19:00:00Z")
    for original in events:
        stored = db.get_event(original.event_id)
        assert stored == replace(original, updated_at="2026-10-07T19:00:00Z")
        assert isinstance(stored.gold_relevance, bool)


def test_duplicates_are_not_inserted(db, parse, feed_text):
    events = usd(parse, feed_text)
    assert db.upsert_events(events) == {"inserted": 23, "updated": 0, "unchanged": 0}
    assert db.upsert_events(events) == {"inserted": 0, "updated": 0, "unchanged": 23}
    # A separately parsed copy of the same feed maps onto the same rows.
    assert db.upsert_events(usd(parse, feed_text)) == {"inserted": 0, "updated": 0, "unchanged": 23}
    assert db.count() == 23


def test_actual_updates_existing_event(loaded_db):
    before = claims(loaded_db)
    assert before.actual is None

    released = replace(before, actual="3.1%", retrieved_at="2026-10-08T12:31:00Z")
    assert loaded_db.upsert_event(released, now="2026-10-08T12:31:05Z") == "updated"

    after = claims(loaded_db)
    assert loaded_db.count() == 23
    assert after.event_id == before.event_id
    assert after.actual == "3.1%"
    assert after.updated_at == "2026-10-08T12:31:05Z"
    assert after.retrieved_at == "2026-10-08T12:31:00Z"


def test_set_actual(loaded_db):
    event = claims(loaded_db)
    assert loaded_db.set_actual(event.event_id, "205K")
    assert claims(loaded_db).actual == "205K"
    assert not loaded_db.set_actual("ff-does-not-exist", "1")


def test_known_actual_survives_a_later_sync_without_it(loaded_db, parse, feed_text):
    loaded_db.set_actual(claims(loaded_db).event_id, "205K")
    loaded_db.upsert_events(usd(parse, feed_text))
    assert claims(loaded_db).actual == "205K"


def test_revised_forecast_updates_in_place(loaded_db, parse, feed_text):
    before = claims(loaded_db)
    revised = json.loads(feed_text)
    for record in revised:
        if record["title"] == "Unemployment Claims":
            record["forecast"] = "210K"
    counts = loaded_db.upsert_events(usd(parse, json.dumps(revised)), now="2026-10-08T01:00:00Z")
    assert counts == {"inserted": 0, "updated": 1, "unchanged": 22}
    assert claims(loaded_db).forecast == "210K"
    assert claims(loaded_db).updated_at == "2026-10-08T01:00:00Z" != before.updated_at


def test_unchanged_event_keeps_updated_at_but_refreshes_retrieved_at(db, parse, feed_text):
    events = usd(parse, feed_text)
    db.upsert_events(events, now="2026-10-07T19:00:00Z")
    later = [replace(e, retrieved_at="2026-10-07T20:00:00Z") for e in events]
    db.upsert_events(later, now="2026-10-07T20:00:05Z")
    stored = claims(db)
    assert stored.updated_at == "2026-10-07T19:00:00Z"
    assert stored.retrieved_at == "2026-10-07T20:00:00Z"


def test_rescheduled_event_replaces_the_old_row(loaded_db, parse, feed_text):
    moved = json.loads(feed_text)
    for record in moved:
        if record["title"] == "Unemployment Claims":
            record["date"] = "2026-10-09T08:30:00-04:00"
    all_events = parse(json.dumps(moved)).events
    events = [e for e in all_events if e.currency == "USD"]
    loaded_db.upsert_events(events)
    instants = [e.datetime_utc for e in all_events]
    removed = loaded_db.delete_missing((e.event_id for e in events), min(instants), max(instants))
    assert removed == 1
    assert loaded_db.count() == 23
    assert claims(loaded_db).date == "2026-10-09"


def test_delete_missing_leaves_other_weeks_alone(loaded_db):
    assert loaded_db.delete_missing([], "2026-10-11T00:00:00Z", "2026-10-17T23:59:59Z") == 0
    assert loaded_db.count() == 23


def test_database_persists_on_disk(tmp_path, parse, feed_text):
    path = tmp_path / "nested" / "events.db"
    with Database(path) as first:
        first.upsert_events(usd(parse, feed_text))
    with Database(path) as second:
        assert second.count() == 23
