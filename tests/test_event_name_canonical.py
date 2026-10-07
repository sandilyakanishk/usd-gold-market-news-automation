"""The Forex Factory event name is the canonical name through Steps 1 to 4.

Feed -> parser -> database -> classification -> actual enrichment -> message:
`event_name` must be byte-for-byte what Forex Factory sent at every stage, on
every backend. The headline is a different field and is free to change.
"""

import inspect
import json
from dataclasses import fields, replace
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from src.actuals.mapping import ActualEventMapping
from src.actuals.models import ActualRecord
from src.actuals.service import enrich_actuals, load_enriched
from src.classification.models import Classification
from src.classification.service import classify_events
from src.collector.parser import make_event_id, parse_feed
from src.config import PROJECT_ROOT
from src.content.builder import build_content_builder
from src.content.models import Message
from src.database import base

from .test_actuals_service import FixtureProvider
from .test_repository_contract import repo  # noqa: F401  (fixture)

D = Decimal

# name -> (Forex Factory timestamp, impact, forecast, previous)
FEED = {
    "CPI m/m": ("2026-10-14T08:30:00-04:00", "High", "0.3%", "0.4%"),
    "FOMC Meeting Minutes": ("2026-10-07T14:00:00-04:00", "High", "", ""),
    "Federal Funds Rate": ("2026-10-28T14:00:00-04:00", "High", "4.00%", "4.00%"),
    "Unemployment Rate": ("2026-10-02T08:30:00-04:00", "High", "4.1%", "4.1%"),
    "Retail Sales m/m": ("2026-10-16T08:30:00-04:00", "High", "0.4%", "1.1%"),
    "Non-Farm Employment Change": ("2026-10-02T08:30:00-04:00", "High", "50K", "22K"),
}
NAMES = sorted(FEED)
BEFORE = datetime(2026, 10, 1, 3, 30, tzinfo=timezone.utc)    # nothing released yet
AFTER = datetime(2026, 10, 30, 3, 30, tzinfo=timezone.utc)    # everything released


def feed_text():
    return json.dumps([{"title": name, "country": "USD", "date": when, "impact": impact, "forecast": fc, "previous": pv}
                       for name, (when, impact, fc, pv) in FEED.items()]
                      + [{"title": "CPI m/m", "country": "EUR", "date": "2026-10-14T05:00:00-04:00", "impact": "High",
                          "forecast": "0.2%", "previous": "0.1%"}])


def sources():
    return {
        "BLS": FixtureProvider("BLS", {
            "CUSR0000SA0": {date(2026, 8, 1): D("332.813"), date(2026, 9, 1): D("334.800")},   # +0.6%
            "CES0000000001": {date(2026, 8, 1): D("159015"), date(2026, 9, 1): D("159044")},   # +29K
            "LNS14000000": {date(2026, 9, 1): D("4.2")},
        }),
        "FRED": FixtureProvider("FRED", {
            "RSAFS": {date(2026, 8, 1): D("737763"), date(2026, 9, 1): D("739238")},           # +0.2%
            "DFEDTARU": {date(2026, 10, 29): D("3.75")},                                         # cut from 4.00%
        }),
    }


@pytest.fixture
def pipeline(repo, settings, parse):  # noqa: F811
    """Run Steps 1 to 4 and capture the event names seen at every stage."""
    settings = replace(settings, actuals_retry_days=90)
    stage = {}

    # Step 1: parse the feed and store the USD events.
    parsed = [e for e in parse(feed_text()).events if e.currency == "USD"]
    stage["parser"] = {e.event_id: e.event_name for e in parsed}
    repo.upsert_events(parsed)
    stage["stored"] = {e.event_id: e.event_name for e in repo.query_events()}

    # Step 2: classify.
    classify_events(settings, repo)
    stage["classified"] = {e.event_id: e.event_name for e in repo.query_events()}

    # Step 4, before any release.
    builder = build_content_builder(settings, BEFORE)
    before_items = load_enriched(repo, now=BEFORE)
    stage["messages_before"] = builder.upcoming_reminders(before_items) + builder.high_alerts(before_items)

    # Step 3: enrich with the released values.
    enrich_actuals(settings, repo, providers=sources(), now=AFTER)
    stage["enriched"] = {e.event_id: e.event_name for e in repo.query_events()}
    stage["records"] = repo.get_actual_records(list(stage["enriched"]))

    # A later calendar sync and a reclassification must not disturb anything either.
    repo.upsert_events([e for e in parse(feed_text()).events if e.currency == "USD"])
    classify_events(settings, repo)
    stage["resynced"] = {e.event_id: e.event_name for e in repo.query_events()}

    # Step 4, after the releases.
    after_items = load_enriched(repo, now=AFTER)
    stage["messages_after"] = build_content_builder(settings, AFTER).actual_results(after_items)
    stage["events_after"] = {e.event_id: e for e in repo.query_events()}
    return stage


def test_event_name_is_identical_at_every_stage(pipeline):
    expected = {make_event_id(when, "USD", name): name for name, (when, *_rest) in FEED.items()}
    for stage in ("parser", "stored", "classified", "enriched", "resynced"):
        assert pipeline[stage] == expected, stage
    assert sorted(expected.values()) == NAMES


@pytest.mark.parametrize("name", NAMES)
def test_canonical_name_through_steps_1_to_4(pipeline, name):
    when = FEED[name][0]
    event_id = make_event_id(when, "USD", name)
    for stage in ("parser", "stored", "classified", "enriched", "resynced"):
        assert pipeline[stage][event_id] == name, stage

    before = [m for m in pipeline["messages_before"] if m.event_id == event_id]
    assert before, "expected at least one pre-release message"
    for message in before:
        assert message.event_name == name and f"*{name}*" in message.text
        assert message.headline and message.headline != name and name not in message.headline

    after = [m for m in pipeline["messages_after"] if m.event_id == event_id]
    if name == "FOMC Meeting Minutes":
        assert after == []  # no numeric result, so no result message
        return
    (result,) = after
    assert result.event_name == name and f"*{name}*" in result.text
    assert result.headline != name and name not in result.headline
    # The headline moved on; the name did not.
    assert result.headline not in {m.headline for m in before}
    assert {m.event_name for m in before} == {result.event_name} == {name}


def test_headlines_before_and_after(pipeline):
    before = {m.event_name: m.headline for m in pipeline["messages_before"] if m.message_type == "UPCOMING_REMINDER"}
    after = {m.event_name: m.headline for m in pipeline["messages_after"]}
    assert before == {
        "CPI m/m": "🇺🇸 US CPI inflation data due on 14 Oct",
        "FOMC Meeting Minutes": "🇺🇸 Fed meeting minutes due on 7 Oct",
        "Federal Funds Rate": "🚨 Fed interest-rate decision due on 28 Oct",
        "Non-Farm Employment Change": "🇺🇸 US non-farm payrolls report due tomorrow",
        "Retail Sales m/m": "🇺🇸 US retail sales data due on 16 Oct",
        "Unemployment Rate": "🇺🇸 US unemployment rate due tomorrow",
    }
    assert after == {
        "CPI m/m": "🇺🇸 US CPI comes in above expectations",
        "Federal Funds Rate": "🚨 Fed cuts interest rates to 3.75%",
        "Non-Farm Employment Change": "🇺🇸 US non-farm payrolls come in below expectations",
        "Retail Sales m/m": "🇺🇸 US retail sales come in below expectations",
        "Unemployment Rate": "🇺🇸 US unemployment rate comes in above expectations",
    }


def test_provider_names_never_reach_event_name(pipeline):
    mapping = ActualEventMapping.from_file(PROJECT_ROOT / "config" / "actual_event_mapping.json")
    provider_texts = {m.series_id for m in mapping.mappings} | {m.series_name for m in mapping.mappings} \
        | {m.source_event for m in mapping.mappings}
    events, records = pipeline["events_after"], pipeline["records"]
    released = [r for r in records.values() if r.release_status == "RELEASED"]
    assert len(released) == 5
    for record in released:
        event = events[record.event_id]
        assert record.actual_source_event in provider_texts       # the provider's name is kept here...
        assert event.event_name in FEED                            # ...and the event keeps Forex Factory's
        assert event.event_name not in provider_texts
        assert not any(text in event.event_name for text in provider_texts)
    cpi = events[make_event_id(FEED["CPI m/m"][0], "USD", "CPI m/m")]
    assert cpi.event_name == "CPI m/m"
    assert records[cpi.event_id].actual_source_event == "CUSR0000SA0: CPI-U, all items, seasonally adjusted, 1-month percent change"
    for message in pipeline["messages_after"]:
        assert not any(text in message.text for text in provider_texts)   # series names stay out of the copy


def test_nothing_but_the_actual_changed_on_the_events(pipeline, parse):
    original = {e.event_id: e for e in parse(feed_text()).events if e.currency == "USD"}
    for event_id, stored in pipeline["events_after"].items():
        source = original[event_id]
        for column in ("event_id", "event_name", "date", "time", "timezone", "datetime_utc", "currency", "impact",
                       "original_impact", "gold_relevance", "forecast", "previous", "source"):
            assert getattr(stored, column) == getattr(source, column), column


def test_event_id_is_derived_from_the_name_so_a_name_cannot_drift():
    assert make_event_id("2026-10-14T08:30:00-04:00", "USD", "CPI m/m") != \
        make_event_id("2026-10-14T08:30:00-04:00", "USD", "US inflation data")


# -- structure: there is nowhere for a second name to hide ------------------------------------

def test_derived_tables_have_no_event_name_column():
    assert "event_name" not in base.CLASSIFICATION_COLUMNS and "event_name" not in base.ACTUALS_COLUMNS
    assert "headline" not in base.ALL_COLUMNS + base.CLASSIFICATION_COLUMNS + base.ACTUALS_COLUMNS
    assert not any("name" in f.name for f in fields(Classification))
    assert not any(f.name in ("event_name", "headline") for f in fields(ActualRecord))


def test_no_statement_can_rewrite_an_event_name():
    source = inspect.getsource(base)
    updates = [line.strip() for line in source.splitlines() if "UPDATE" in line and "SET" in line and "DO UPDATE" not in line]
    assert updates and all("SET actual = ?, updated_at = ?" in line for line in updates)


def test_message_keeps_event_name_and_headline_as_separate_fields():
    names = {f.name for f in fields(Message)}
    assert {"event_name", "headline"} <= names


def test_the_parser_keeps_forex_factory_titles_verbatim(parse, feed_text):
    raw = json.loads(feed_text)
    parsed = parse(feed_text).events
    assert [e.event_name for e in parsed] == [r["title"] for r in raw]
