import pytest

from src.filters import gold_usd_filters as f
from src.filters.gold_usd_filters import GoldRelevance


def names(events):
    return [e.event_name for e in events]


# -- gold relevance classification ---------------------------------------------

@pytest.mark.parametrize("name", [
    "Non-Farm Employment Change", "ADP Non-Farm Employment Change", "CPI m/m", "Core CPI m/m",
    "PPI m/m", "Core PPI m/m", "Federal Funds Rate", "FOMC Statement", "FOMC Press Conference",
    "Fed Chair Powell Speaks", "FOMC Member Waller Speaks", "Advance GDP q/q", "Core PCE Price Index m/m",
    "Retail Sales m/m", "Core Retail Sales m/m", "Unemployment Claims", "Unemployment Rate",
    "Average Hourly Earnings m/m", "ISM Manufacturing PMI", "ISM Services PMI", "CB Consumer Confidence",
    "Core Durable Goods Orders m/m", "Existing Home Sales", "New Home Sales", "JOLTS Job Openings",
    "Industrial Production m/m", "Flash Manufacturing PMI", "Flash Services PMI",
])
def test_major_us_releases_are_gold_relevant(classifier, name):
    assert classifier.is_relevant("USD", name)


@pytest.mark.parametrize("name", [
    "Crude Oil Inventories", "Natural Gas Storage", "10-y Bond Auction", "Trade Balance", "Bank Holiday",
])
def test_unlisted_usd_events_are_not_gold_relevant(classifier, name):
    assert not classifier.is_relevant("USD", name)


def test_other_currencies_are_never_gold_relevant(classifier):
    assert not classifier.is_relevant("EUR", "CPI m/m")
    assert not classifier.is_relevant("All", "OPEC-JMMC Meetings")


def test_keywords_match_whole_words_only():
    rules = GoldRelevance(["USD"], ["Fed", "PPI"])
    assert rules.is_relevant("USD", "fed chair speaks")
    assert not rules.is_relevant("USD", "Federal Budget Balance")
    assert not rules.is_relevant("USD", "Shipping Index")


def test_relevance_list_is_configurable(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text('{"currencies": ["USD"], "keywords": ["Crude Oil"], "exclude_keywords": ["Inventories"]}')
    rules = GoldRelevance.from_file(path)
    assert rules.is_relevant("USD", "Crude Oil Prices")
    assert not rules.is_relevant("USD", "Crude Oil Inventories")
    assert not rules.is_relevant("USD", "CPI m/m")


def test_gold_relevance_is_independent_of_impact(loaded_db):
    gold = f.get_gold_events(loaded_db)
    assert {e.impact for e in gold} == {"High", "Medium", "Low"}
    # A Medium-impact USD event that is not gold-relevant, and a Low one that is.
    by_name = {e.event_name: e for e in f.get_usd_events(loaded_db)}
    assert (by_name["President Trump Speaks"].impact, by_name["President Trump Speaks"].gold_relevance) == ("Medium", False)
    assert (by_name["FOMC Member Bowman Speaks"].impact, by_name["FOMC Member Bowman Speaks"].gold_relevance) == ("Low", True)


# -- query helpers (fixture week, times in Asia/Kolkata) -------------------------

def test_usd_filter(loaded_db):
    events = f.get_usd_events(loaded_db)
    assert len(events) == 23
    assert {e.currency for e in events} == {"USD"}
    assert events == sorted(events, key=lambda e: (e.date, e.time, e.event_name))


def test_high_impact_usd(loaded_db):
    assert names(f.get_high_impact_usd_events(loaded_db)) == ["FOMC Meeting Minutes"]


def test_medium_impact_usd(loaded_db):
    assert names(f.get_medium_impact_usd_events(loaded_db)) == [
        "ISM Services PMI", "President Trump Speaks", "FOMC Member Waller Speaks", "Unemployment Claims",
        "Prelim UoM Consumer Sentiment", "Prelim UoM Inflation Expectations",
    ]


def test_low_impact_usd(loaded_db):
    events = f.get_low_impact_usd_events(loaded_db)
    assert len(events) == 16 and {e.impact for e in events} == {"Low"}


def test_gold_events(loaded_db):
    events = f.get_gold_events(loaded_db)
    assert all(e.gold_relevance and e.currency == "USD" for e in events)
    assert names(events) == [
        "Final Services PMI", "ISM Services PMI",
        "ADP Weekly Employment Change", "ADP Weekly Employment Change",
        "FOMC Member Bowman Speaks", "FOMC Member Schmid Speaks",
        "FOMC Meeting Minutes",
        "FOMC Member Waller Speaks", "Unemployment Claims", "FOMC Member Musalem Speaks",
        "Prelim UoM Consumer Sentiment", "Prelim UoM Inflation Expectations",
        "FOMC Member Collins Speaks",
    ]


def test_high_impact_gold(loaded_db):
    events = f.get_high_impact_gold_events(loaded_db)
    assert names(events) == ["FOMC Meeting Minutes"]
    assert events[0].is_high_impact and events[0].gold_relevance


def test_medium_impact_gold(loaded_db):
    assert names(f.get_medium_impact_gold_events(loaded_db)) == [
        "ISM Services PMI", "FOMC Member Waller Speaks", "Unemployment Claims",
        "Prelim UoM Consumer Sentiment", "Prelim UoM Inflation Expectations",
    ]


def test_events_for_date(loaded_db):
    events = f.get_events_for_date(loaded_db, "2026-10-08")
    assert {e.date for e in events} == {"2026-10-08"}
    assert names(events) == [
        "Consumer Credit m/m",  # 15:00 New York on the 7th is 00:30 on the 8th in India
        "FOMC Member Waller Speaks", "Unemployment Claims", "Final Wholesale Inventories m/m",
        "Natural Gas Storage", "30-y Bond Auction", "FOMC Member Musalem Speaks",
    ]
    assert f.get_events_for_date(loaded_db, "2030-01-01") == []


def test_events_for_date_with_extra_filters(loaded_db):
    events = f.get_events_for_date(loaded_db, "2026-10-08", gold_relevance=True, impacts=["Medium"])
    assert names(events) == ["FOMC Member Waller Speaks", "Unemployment Claims"]


def test_events_for_date_range_is_inclusive(loaded_db):
    events = f.get_events_for_date_range(loaded_db, "2026-10-06", "2026-10-07")
    assert {e.date for e in events} == {"2026-10-06", "2026-10-07"}
    assert len(f.get_events_for_date_range(loaded_db, "2026-10-01", "2026-10-31")) == 23
    assert names(f.get_high_impact_gold_events(loaded_db, "2026-10-07", "2026-10-07")) == ["FOMC Meeting Minutes"]
    assert f.get_high_impact_gold_events(loaded_db, "2026-10-08", "2026-10-09") == []
