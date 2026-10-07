"""Headline generation: deterministic, state-dependent, and never a substitute for the event name."""

import copy
import json

import pytest

from src.classification.rules import PriorityRules
from src.config import PROJECT_ROOT
from src.content.headlines import STATES, HeadlineConfigError, HeadlineRules

RULES_PATH = PROJECT_ROOT / "config" / "headline_rules.json"


@pytest.fixture(scope="module")
def config():
    return json.loads(RULES_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def rules():
    return HeadlineRules.from_file(RULES_PATH)


def make(rules, name, state, category=None, when="today", **values):
    return rules.headline(event_name=name, category=category, state=state, when=when, **values)


@pytest.mark.parametrize("name, state, expected", [
    ("CPI m/m", "UPCOMING", "🇺🇸 US CPI inflation data due today"),
    ("CPI m/m", "PASSED", "🇺🇸 US CPI inflation data"),
    ("CPI m/m", "ABOVE_FORECAST", "🇺🇸 US CPI comes in above expectations"),
    ("CPI m/m", "BELOW_FORECAST", "🇺🇸 US CPI comes in below expectations"),
    ("CPI m/m", "IN_LINE_WITH_FORECAST", "🇺🇸 US CPI in line with expectations"),
    ("CPI m/m", "RELEASED", "🇺🇸 US CPI released"),
    ("CPI y/y", "UPCOMING", "🇺🇸 US CPI inflation data due today"),
    ("Core CPI m/m", "ABOVE_FORECAST", "🇺🇸 US core CPI comes in above expectations"),
    ("Non-Farm Employment Change", "UPCOMING", "🇺🇸 US non-farm payrolls report due today"),
    ("Non-Farm Employment Change", "BELOW_FORECAST", "🇺🇸 US non-farm payrolls come in below expectations"),
    ("Unemployment Rate", "ABOVE_FORECAST", "🇺🇸 US unemployment rate comes in above expectations"),
    ("Retail Sales m/m", "UPCOMING", "🇺🇸 US retail sales data due today"),
    ("Retail Sales m/m", "ABOVE_FORECAST", "🇺🇸 US retail sales come in above expectations"),
    ("Unemployment Claims", "IN_LINE_WITH_FORECAST", "🇺🇸 US weekly jobless claims in line with expectations"),
    ("Advance GDP q/q", "UPCOMING", "🇺🇸 US GDP growth data (advance estimate) due today"),
    ("Federal Funds Rate", "UPCOMING", "🚨 Fed interest-rate decision due today"),
    ("FOMC Statement", "UPCOMING", "🚨 Fed policy statement due today"),
    ("FOMC Meeting Minutes", "UPCOMING", "🇺🇸 Fed meeting minutes due today"),
    ("FOMC Meeting Minutes", "PASSED", "🇺🇸 Fed meeting minutes published"),
    ("FOMC Member Waller Speaks", "UPCOMING", "🇺🇸 Fed official Waller due to speak today"),
    ("FOMC Member Waller Speaks", "PASSED", "🇺🇸 Fed official Waller speech"),
    ("Fed Chair Powell Speaks", "UPCOMING", "🇺🇸 Fed Chair Powell due to speak today"),
    ("Fed Chair Powell Testifies", "PASSED", "🇺🇸 Fed Chair Powell testimony"),
    ("President Trump Speaks", "UPCOMING", "🇺🇸 President Trump due to speak today"),
    ("Prelim UoM Consumer Sentiment", "UPCOMING", "🇺🇸 US consumer sentiment survey due today"),
    ("Revised UoM Inflation Expectations", "BELOW_FORECAST", "🇺🇸 US consumer inflation expectations come in below expectations"),
])
def test_headline_for_each_state(rules, name, state, expected):
    assert make(rules, name, state) == expected


def test_headline_changes_with_state_while_the_name_does_not(rules):
    name = "CPI m/m"
    headlines = {state: make(rules, name, state) for state in STATES}
    assert len(set(headlines.values())) == len(STATES)
    assert name == "CPI m/m"
    assert all(h != name and name not in h for h in headlines.values())


def test_fed_decision_headline_is_based_on_the_previous_rate(rules):
    common = dict(due=True, release_status="RELEASED", event_name="Federal Funds Rate")
    # Unchanged rate: forecast-based surprise says IN_LINE; the comparison that matters is with the previous rate.
    same = rules.state(surprise_status="ABOVE_FORECAST", actual="4.00%", previous="4.00%", **common)
    cut = rules.state(surprise_status="IN_LINE_WITH_FORECAST", actual="4.00%", previous="4.25%", **common)
    hike = rules.state(surprise_status="IN_LINE_WITH_FORECAST", actual="4.50%", previous="4.25%", **common)
    unknown = rules.state(surprise_status="IN_LINE_WITH_FORECAST", actual="4.00%", previous=None, **common)
    assert (same, cut, hike, unknown) == ("IN_LINE_WITH_FORECAST", "BELOW_FORECAST", "ABOVE_FORECAST", "RELEASED")
    assert make(rules, "Federal Funds Rate", same, actual="4.00%") == "🚨 Fed leaves interest rates unchanged at 4.00%"
    assert make(rules, "Federal Funds Rate", cut, actual="4.00%") == "🚨 Fed cuts interest rates to 4.00%"
    assert make(rules, "Federal Funds Rate", hike, actual="4.50%") == "🚨 Fed raises interest rates to 4.50%"
    assert make(rules, "Federal Funds Rate", unknown, actual="4.00%") == "🚨 Fed interest-rate decision announced: 4.00%"


@pytest.mark.parametrize("due, status, surprise, actual, expected", [
    (False, "UPCOMING", "NOT_AVAILABLE", None, "UPCOMING"),
    (True, "NO_DATA", "NOT_AVAILABLE", None, "PASSED"),
    (True, "FAILED", "NOT_AVAILABLE", None, "PASSED"),
    (True, "RELEASED", "ABOVE_FORECAST", "0.5%", "ABOVE_FORECAST"),
    (True, "RELEASED", "BELOW_FORECAST", "0.1%", "BELOW_FORECAST"),
    (True, "RELEASED", "IN_LINE_WITH_FORECAST", "0.3%", "IN_LINE_WITH_FORECAST"),
    (True, "RELEASED", "NOT_AVAILABLE", "0.3%", "RELEASED"),
    (True, "RELEASED", "ABOVE_FORECAST", None, "PASSED"),   # released flag without a value is not trusted
    (True, None, None, None, "PASSED"),
])
def test_state_selection(rules, due, status, surprise, actual, expected):
    assert rules.state(event_name="CPI m/m", due=due, release_status=status, surprise_status=surprise,
                       actual=actual, previous="0.4%") == expected


@pytest.mark.parametrize("days, expected", [(0, "today"), (1, "tomorrow"), (2, "on 14 Nov"), (9, "on 14 Nov"), (-1, "on 14 Nov")])
def test_when_phrase(rules, days, expected):
    assert rules.when_phrase(days, "14 Nov") == expected


def test_unlisted_events_use_their_category_and_never_echo_the_name(rules):
    assert make(rules, "Existing Home Sales", "UPCOMING", "HOUSING") == "🇺🇸 US housing data due today"
    assert make(rules, "Existing Home Sales", "ABOVE_FORECAST", "HOUSING") == "🇺🇸 US housing figure comes in above expectations"
    assert make(rules, "Crude Oil Inventories", "PASSED", "ENERGY") == "🇺🇸 US energy inventory data"
    assert make(rules, "A Brand New Indicator m/m", "UPCOMING", "OTHER") == "🇺🇸 US economic data due today"
    assert make(rules, "A Brand New Indicator m/m", "UPCOMING", None) == "🇺🇸 US economic data due today"


def test_no_headline_is_awkward_empty_or_just_the_event_name(rules, feed_text):
    priority_rules = PriorityRules.from_file(PROJECT_ROOT / "config" / "gold_priority_rules.json")
    names = sorted({e["title"] for e in json.loads(feed_text) if e["country"] == "USD"}) + [
        "CPI m/m", "CPI y/y", "Core CPI m/m", "PPI m/m", "Core PCE Price Index m/m", "Non-Farm Employment Change",
        "Average Hourly Earnings m/m", "Federal Funds Rate", "Advance GDP q/q", "Retail Sales m/m", "Unknown Thing q/q"]
    for name in names:
        category = priority_rules.classify("x", "USD", name, "Medium").category
        for state in STATES:
            headline = make(rules, name, state, category, actual="1.0%")
            assert headline.strip() and headline != name
            assert name.casefold() not in headline.casefold(), (name, headline)
            for junk in ("m/m", "y/y", "q/q", "{", "}", "  ", "None", "null"):
                assert junk not in headline, (name, state, headline)
            words = headline.casefold().split()
            assert not any(a == b for a, b in zip(words, words[1:])), headline  # no doubled word


def test_headlines_carry_no_trading_language(rules, config):
    text = json.dumps(config, ensure_ascii=False).casefold()
    for word in ["bullish", "bearish", "buy", "sell", "will rise", "will fall", "guaranteed", "explode", "surge", "crash"]:
        assert word not in text, word


def test_headlines_are_deterministic(rules):
    again = HeadlineRules.from_file(RULES_PATH)
    for name in ("CPI m/m", "FOMC Member Waller Speaks", "Unknown"):
        for state in STATES:
            assert make(rules, name, state, "INFLATION") == make(again, name, state, "INFLATION")


def test_wording_is_configurable(config):
    custom = copy.deepcopy(config)
    custom["patterns"]["ABOVE_FORECAST"] = "{flag} {short}: higher than expected"
    custom["rules"].insert(0, {"events": ["Existing Home Sales"], "subject": "US existing home sales", "short": "US existing home sales"})
    r = HeadlineRules(custom)
    assert make(r, "CPI m/m", "ABOVE_FORECAST") == "🇺🇸 US CPI: higher than expected"
    assert make(r, "Existing Home Sales", "UPCOMING", "HOUSING") == "🇺🇸 US existing home sales due today"


@pytest.mark.parametrize("mutate, message", [
    (lambda c: c.pop("patterns"), "malformed"),
    (lambda c: c["patterns"].pop("PASSED"), "malformed"),
    (lambda c: c["patterns"].update(UPCOMING="{flag} {event_name} due"), "Unknown placeholder"),
    (lambda c: c["patterns"].update(UPCOMING="   "), "Empty headline text"),
    (lambda c: c["rules"].append({"events": ["cpi M/M"], "subject": "x", "short": "y"}), "more than one headline rule"),
    (lambda c: c["rules"].append({"events": ["New Event"], "subject": "x"}), "give 'subject' and 'short'"),
    (lambda c: c["rules"].append({"events": ["New Event"], "headlines": {"UPCOMING": "a", "LATER": "b"}}), "Unknown headline state"),
    (lambda c: c["rules"].append({"events": ["New Event"], "headlines": {"UPCOMING": "{1} a", "PASSED": "b"}}), "Unknown placeholder"),
    (lambda c: c["rules"][0].update(compare_to="consensus"), "compare_to must be"),
    (lambda c: c["default"].pop("short"), "needs both"),
    (lambda c: c["when"].update(today=""), "must not be empty"),
])
def test_broken_headline_files_are_rejected(config, mutate, message):
    custom = copy.deepcopy(config)
    mutate(custom)
    with pytest.raises(HeadlineConfigError, match=message):
        HeadlineRules(custom)


def test_unreadable_headline_file(tmp_path):
    with pytest.raises(HeadlineConfigError, match="Cannot read"):
        HeadlineRules.from_file(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(HeadlineConfigError, match="not valid JSON"):
        HeadlineRules.from_file(bad)
