import json

import pytest

from src.collector.parser import MalformedFeedError, make_event_id, normalize_impact


def by_name(events, name):
    return [e for e in events if e.event_name == name]


def test_parses_every_record_of_the_real_fixture(parse, feed_text):
    result = parse(feed_text)
    assert len(result.events) == 83
    assert result.skipped == 0
    assert len({e.event_id for e in result.events}) == 83


def test_event_is_normalized(parse, feed_text):
    (event,) = by_name(parse(feed_text).events, "Unemployment Claims")
    assert event.to_dict() == {
        "event_id": make_event_id("2026-10-08T08:30:00-04:00", "USD", "Unemployment Claims"),
        "date": "2026-10-08",
        "time": "18:00",  # 08:30 New York == 18:00 India
        "timezone": "Asia/Kolkata",
        "datetime_utc": "2026-10-08T12:30:00Z",
        "currency": "USD",
        "event_name": "Unemployment Claims",
        "impact": "Medium",
        "original_impact": "Medium",
        "gold_relevance": True,
        "forecast": "200K",
        "previous": "197K",
        "actual": None,
        "source": "Forex Factory",
        "source_url": "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
        "retrieved_at": "2026-10-07T18:30:27Z",
        "updated_at": None,
    }


def test_without_display_timezone_the_feed_offset_is_kept(parse, feed_text):
    (event,) = by_name(parse(feed_text, display_tz=None).events, "Unemployment Claims")
    assert (event.date, event.time, event.timezone) == ("2026-10-08", "08:30", "UTC-04:00")


def test_timezone_is_null_when_source_gives_no_offset(parse):
    feed = json.dumps([{"title": "CPI m/m", "country": "USD", "date": "2026-10-08T08:30:00", "impact": "High"}])
    (event,) = parse(feed).events
    assert (event.date, event.time) == ("2026-10-08", "08:30")
    assert event.timezone is None and event.datetime_utc is None


def test_missing_forecast_previous_and_actual_are_null(parse, feed_text):
    events = parse(feed_text).events
    (minutes,) = by_name(events, "FOMC Meeting Minutes")
    assert minutes.forecast is None and minutes.previous is None and minutes.actual is None
    (expectations,) = by_name(events, "Prelim UoM Inflation Expectations")
    assert expectations.forecast is None and expectations.previous == "4.6%"
    # The export carries no Actual column at all.
    assert all(e.actual is None for e in events)


def test_absent_keys_and_whitespace_become_null(parse):
    feed = json.dumps([{"title": "CPI m/m", "country": "USD", "date": "2026-10-08T08:30:00-04:00",
                        "impact": "High", "forecast": "   "}])
    (event,) = parse(feed).events
    assert event.forecast is None and event.previous is None and event.actual is None


def test_actual_is_read_if_the_source_ever_provides_it(parse):
    feed = json.dumps([{"title": "CPI m/m", "country": "USD", "date": "2026-10-08T08:30:00-04:00",
                        "impact": "High", "actual": "0.3%"}])
    assert parse(feed).events[0].actual == "0.3%"


@pytest.mark.parametrize("raw, expected", [
    ("High", "High"), ("Medium", "Medium"), ("Low", "Low"), ("Holiday", "Holiday"),
    ("HIGH", "High"), (" low ", "Low"),
    ("Non-Economic", "None"), ("", "None"), (None, "None"), ("Something New", "None"),
])
def test_impact_normalization(raw, expected):
    assert normalize_impact(raw) == expected


def test_original_impact_is_preserved_even_when_unrecognized(parse):
    feed = json.dumps([{"title": "X", "country": "USD", "date": "2026-10-08T08:30:00-04:00",
                        "impact": "Non-Economic"}])
    (event,) = parse(feed).events
    assert (event.impact, event.original_impact) == ("None", "Non-Economic")


@pytest.mark.parametrize("text", ["", "not json", '{"title": "x"}', '[{"title": "x"', "null", "42"])
def test_malformed_feed_raises(parse, text):
    with pytest.raises(MalformedFeedError):
        parse(text)


def test_malformed_records_are_skipped_not_fatal(parse):
    good = {"title": "CPI m/m", "country": "USD", "date": "2026-10-08T08:30:00-04:00", "impact": "High"}
    feed = json.dumps([
        good,
        "just a string",
        {"country": "USD", "date": "2026-10-08T08:30:00-04:00"},  # no title
        {"title": "No date", "country": "USD"},
        {"title": "Bad date", "country": "USD", "date": "next tuesday"},
        {"title": "No currency", "date": "2026-10-08T08:30:00-04:00"},
    ])
    result = parse(feed)
    assert [e.event_name for e in result.events] == ["CPI m/m"]
    assert result.skipped == 5


def test_event_id_is_deterministic_and_independent_of_display_timezone(parse, feed_text):
    ist = {e.event_id for e in parse(feed_text).events}
    raw = {e.event_id for e in parse(feed_text, display_tz=None).events}
    assert ist == raw


def test_identical_source_rows_get_distinct_ids(parse):
    row = {"title": "FOMC Member Speaks", "country": "USD", "date": "2026-10-08T08:30:00-04:00", "impact": "Low"}
    first, second = parse(json.dumps([row, row])).events
    assert first.event_id != second.event_id
    # ...and the assignment is stable across runs.
    assert [e.event_id for e in parse(json.dumps([row, row])).events] == [first.event_id, second.event_id]
