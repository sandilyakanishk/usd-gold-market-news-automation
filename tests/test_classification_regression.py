"""Regression suite for the shipped rule file, using synthetic event records.

These events do not need to be in the current Forex Factory week. Each record
is stored, classified through the normal service, and read back, on SQLite
always and on PostgreSQL when TEST_DATABASE_URL is set.

Names are Forex Factory's own calendar titles (NFP is "Non-Farm Employment
Change", the rate decision is "Federal Funds Rate").
"""

import json

import pytest

from src.classification.rules import PriorityRules
from src.classification.service import classify_events, load_classified
from src.collector.models import Event
from src.collector.parser import make_event_id
from src.config import PROJECT_ROOT

from .test_repository_contract import repo  # noqa: F401  (fixture)

RULES_PATH = PROJECT_ROOT / "config" / "gold_priority_rules.json"
NOW = "2026-11-01T00:00:00Z"

# (event name, Forex Factory impact, category)
CRITICAL_EVENTS = [
    ("CPI m/m", "High", "INFLATION"),
    ("CPI y/y", "High", "INFLATION"),
    ("Core CPI m/m", "High", "INFLATION"),
    ("Non-Farm Employment Change", "High", "EMPLOYMENT"),
    ("Unemployment Rate", "High", "EMPLOYMENT"),
    ("Average Hourly Earnings m/m", "High", "EMPLOYMENT"),
    ("Federal Funds Rate", "High", "MONETARY_POLICY"),
    ("FOMC Statement", "High", "MONETARY_POLICY"),
    ("FOMC Press Conference", "High", "MONETARY_POLICY"),
    ("FOMC Economic Projections", "High", "MONETARY_POLICY"),
    ("Fed Chair Powell Speaks", "High", "FED_COMMUNICATION"),
    ("Fed Chair Powell Testifies", "High", "FED_COMMUNICATION"),
    ("Core PCE Price Index m/m", "High", "INFLATION"),
]

# (event name, impact, level, category, priority, score) -- none may be highlighted
NOT_CRITICAL_EVENTS = [
    # HIGH priority, no explicit highlight rule
    ("PCE Price Index m/m", "High", "STRONG", "INFLATION", "HIGH", 85),
    ("PPI m/m", "High", "STRONG", "INFLATION", "HIGH", 85),
    ("Core PPI m/m", "High", "STRONG", "INFLATION", "HIGH", 85),
    ("Retail Sales m/m", "High", "STRONG", "CONSUMER_ACTIVITY", "HIGH", 85),
    ("Core Retail Sales m/m", "High", "STRONG", "CONSUMER_ACTIVITY", "HIGH", 85),
    ("Advance GDP q/q", "High", "STRONG", "GROWTH", "HIGH", 85),
    ("ADP Non-Farm Employment Change", "High", "MODERATE", "EMPLOYMENT", "HIGH", 70),
    ("Unemployment Claims", "High", "MODERATE", "EMPLOYMENT", "HIGH", 70),
    ("JOLTS Job Openings", "High", "MODERATE", "EMPLOYMENT", "HIGH", 70),
    ("ISM Manufacturing PMI", "High", "MODERATE", "MANUFACTURING", "HIGH", 70),
    ("ISM Services PMI", "High", "MODERATE", "SERVICES", "HIGH", 70),
    # MEDIUM priority
    ("CB Consumer Confidence", "High", "MODERATE", "SENTIMENT", "MEDIUM", 55),
    ("Revised UoM Consumer Sentiment", "Medium", "MODERATE", "SENTIMENT", "MEDIUM", 45),
    ("Durable Goods Orders m/m", "Medium", "MODERATE", "BUSINESS_ACTIVITY", "MEDIUM", 45),
    ("FOMC Member Williams Speaks", "Medium", "MODERATE", "FED_COMMUNICATION", "MEDIUM", 45),
    ("Beige Book", "Medium", "MODERATE", "FED_COMMUNICATION", "MEDIUM", 45),
    ("Existing Home Sales", "High", "WEAK", "HOUSING", "MEDIUM", 40),
    ("Philly Fed Manufacturing Index", "High", "WEAK", "MANUFACTURING", "MEDIUM", 40),
    # LOW priority
    ("Existing Home Sales", "Medium", "WEAK", "HOUSING", "LOW", 30),
    ("New Home Sales", "Medium", "WEAK", "HOUSING", "LOW", 30),
    ("Pending Home Sales m/m", "Medium", "WEAK", "HOUSING", "LOW", 30),
    ("Final Services PMI", "Low", "WEAK", "SERVICES", "LOW", 20),
    ("Flash Manufacturing PMI", "Medium", "WEAK", "MANUFACTURING", "LOW", 30),
    ("Philly Fed Manufacturing Index", "Medium", "WEAK", "MANUFACTURING", "LOW", 30),
    ("Empire State Manufacturing Index", "Medium", "WEAK", "MANUFACTURING", "LOW", 30),
    ("Richmond Manufacturing Index", "Low", "WEAK", "MANUFACTURING", "LOW", 20),
    ("ADP Weekly Employment Change", "Low", "WEAK", "EMPLOYMENT", "LOW", 20),
    ("Atlanta Fed GDPNow", "High", "NONE", "GROWTH", "LOW", 30),
    ("Fed Bank Stress Test Results", "High", "NONE", "OTHER", "LOW", 30),
    ("Crude Oil Inventories", "High", "NONE", "ENERGY", "LOW", 30),
    ("10-y Bond Auction", "Medium", "NONE", "GOVERNMENT_FISCAL", "LOW", 20),
    ("Trade Balance", "High", "NONE", "TRADE", "LOW", 30),
]


def make_event(index: int, name: str, impact: str) -> Event:
    """A synthetic stored event, shaped like the ones the collector produces."""
    raw_date = f"2026-11-{index % 27 + 1:02d}T08:30:00-05:00"
    return Event(
        event_id=make_event_id(raw_date, "USD", f"{name}|{impact}"),
        date=raw_date[:10], time="19:00", timezone="Asia/Kolkata",
        datetime_utc=f"{raw_date[:10]}T13:30:00Z",
        currency="USD", event_name=name, impact=impact, original_impact=impact,
        gold_relevance=False,  # the Step 1 keyword flag plays no part in classification
        forecast=None, previous=None, actual=None,
        source="Forex Factory", source_url="synthetic-test-fixture", retrieved_at=NOW,
    )


@pytest.fixture
def classified(repo, settings):  # noqa: F811
    """Every synthetic event stored and classified once; returns {(name, impact): classification}."""
    cases = [(n, i) for n, i, _ in CRITICAL_EVENTS] + [(n, i) for n, i, *_ in NOT_CRITICAL_EVENTS]
    cases += [("FOMC Meeting Minutes", "High"), ("CPI m/m", "Medium"), ("CPI m/m", "Low"),
              ("Fed Chair Powell Speaks", "Medium"), ("Non-Farm Employment Change", "Low")]
    events = [make_event(index, name, impact) for index, (name, impact) in enumerate(cases)]
    assert len({e.event_id for e in events}) == len(events)
    repo.upsert_events(events, now=NOW)
    result = classify_events(settings, repo, now=NOW)
    assert (result.events, result.inserted, result.unmatched) == (len(events), len(events), [])
    rows = load_classified(repo)
    assert len(rows) == len(events) == repo.count_classifications()
    return {(r.event.event_name, r.event.impact): r.classification for r in rows}


# -- the rule file itself ---------------------------------------------------------

def test_scoring_configuration_is_the_documented_one():
    scoring = json.loads(RULES_PATH.read_text(encoding="utf-8"))["scoring"]
    assert scoring["impact_points"] == {"High": 30, "Medium": 20, "Low": 10, "Holiday": 0, "None": 0}
    assert scoring["gold_relevance_points"] == {"STRONG": 40, "MODERATE": 25, "WEAK": 10, "NONE": 0}
    assert scoring["importance_points"] == {"critical_event": 30, "high_priority_event": 15, "other": 0}
    assert scoring["priority_thresholds"] == {"CRITICAL": 90, "HIGH": 65, "MEDIUM": 40}
    assert scoring["max_priority_by_gold_relevance"] == {"NONE": "LOW", "WEAK": "MEDIUM", "MODERATE": "HIGH"}


def test_highlight_configuration_is_the_documented_one():
    highlight = json.loads(RULES_PATH.read_text(encoding="utf-8"))["highlight"]
    assert highlight["priorities"] == ["CRITICAL"]
    assert highlight["high_priority_events"] == ["FOMC Meeting Minutes"]


@pytest.mark.parametrize("score, priority", [
    (100, "CRITICAL"), (90, "CRITICAL"), (89, "HIGH"), (65, "HIGH"), (64, "MEDIUM"), (40, "MEDIUM"), (39, "LOW"), (0, "LOW"),
])
def test_threshold_boundaries(score, priority):
    """Feed exact scores through the engine by making impact carry the whole score."""
    config = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    config["scoring"]["impact_points"] = {"High": score}
    config["scoring"]["gold_relevance_points"] = {"STRONG": 0, "MODERATE": 0, "WEAK": 0, "NONE": 0}
    config["scoring"]["importance_points"] = {"critical_event": 0, "high_priority_event": 0, "other": 0}
    c = PriorityRules(config).classify("x", "USD", "CPI m/m", "High")
    assert (c.priority_score, c.priority) == (score, priority)


# -- major events -------------------------------------------------------------------

@pytest.mark.parametrize("name, impact, category", CRITICAL_EVENTS)
def test_major_event_is_critical_and_highlighted(classified, name, impact, category):
    c = classified[(name, impact)]
    assert c.gold_relevance_level == "STRONG" and c.gold_relevance is True
    assert c.category == category
    assert (c.priority, c.priority_score) == ("CRITICAL", 100)
    assert c.highlight_required is True
    assert c.classification_reason == (
        "Priority CRITICAL: score 100/100 = Forex Factory impact High (30) + Gold relevance STRONG (40) "
        "+ critical event (30).")
    assert c.classification_version == "1.0.0"


def test_critical_events_stay_critical_at_medium_impact_but_not_at_low(classified):
    """Impact is one factor of three: it can lower a major event, it cannot make a minor one critical."""
    for name in ("CPI m/m", "Fed Chair Powell Speaks"):
        medium = classified[(name, "Medium")]
        assert (medium.priority, medium.priority_score, medium.highlight_required) == ("CRITICAL", 90, True)
    for name in ("CPI m/m", "Non-Farm Employment Change"):
        low = classified[(name, "Low")]
        assert (low.gold_relevance_level, low.priority, low.priority_score, low.highlight_required) == (
            "STRONG", "HIGH", 80, False)


def test_headline_pce_is_high_while_core_pce_is_critical(classified):
    """Only Core PCE is in critical_events; headline PCE Price Index is a high-priority event."""
    core, headline = classified[("Core PCE Price Index m/m", "High")], classified[("PCE Price Index m/m", "High")]
    assert (core.priority, core.highlight_required) == ("CRITICAL", True)
    assert (headline.gold_relevance_level, headline.priority, headline.highlight_required) == ("STRONG", "HIGH", False)


# -- everything else ------------------------------------------------------------------

@pytest.mark.parametrize("name, impact, level, category, priority, score", NOT_CRITICAL_EVENTS)
def test_secondary_event_is_not_critical_and_not_highlighted(classified, name, impact, level, category, priority, score):
    c = classified[(name, impact)]
    assert (c.gold_relevance_level, c.category, c.priority, c.priority_score) == (level, category, priority, score)
    assert c.priority != "CRITICAL"
    assert c.highlight_required is False
    assert c.gold_relevance == (level != "NONE")


def test_high_forex_factory_impact_spans_every_priority(classified):
    """Proof that impact alone does not decide: the same High rating lands on all four priorities."""
    by_priority = {}
    for (name, impact), c in classified.items():
        if impact == "High":
            by_priority.setdefault(c.priority, []).append(name)
    assert set(by_priority) == {"CRITICAL", "HIGH", "MEDIUM", "LOW"}
    assert "Crude Oil Inventories" in by_priority["LOW"]
    assert "Existing Home Sales" in by_priority["MEDIUM"]
    assert "Retail Sales m/m" in by_priority["HIGH"]
    assert "CPI m/m" in by_priority["CRITICAL"]


# -- highlight --------------------------------------------------------------------------

def test_highlight_matrix(classified):
    # 1. CRITICAL -> highlighted
    assert classified[("Federal Funds Rate", "High")].highlight_required is True
    # 2. HIGH without an explicit highlight rule -> not highlighted
    for name in ("Retail Sales m/m", "PPI m/m", "Unemployment Claims", "Advance GDP q/q"):
        c = classified[(name, "High")]
        assert (c.priority, c.highlight_required) == ("HIGH", False), name
    # 3. HIGH with an explicit highlight rule -> highlighted
    minutes = classified[("FOMC Meeting Minutes", "High")]
    assert (minutes.gold_relevance_level, minutes.category) == ("STRONG", "FED_COMMUNICATION")
    assert (minutes.priority, minutes.priority_score, minutes.highlight_required) == ("HIGH", 85, True)
    # 4. MEDIUM -> not highlighted
    assert classified[("CB Consumer Confidence", "High")].highlight_required is False
    # 5. LOW -> not highlighted
    assert classified[("Final Services PMI", "Low")].highlight_required is False


def test_every_highlight_is_critical_or_explicitly_listed(classified):
    highlighted = {name: c.priority for (name, _), c in classified.items() if c.highlight_required}
    assert set(highlighted.values()) <= {"CRITICAL", "HIGH"}
    assert [n for n, p in highlighted.items() if p == "HIGH"] == ["FOMC Meeting Minutes"]
    assert all(c.highlight_required for c in classified.values() if c.priority == "CRITICAL")
    assert not any(c.highlight_required for c in classified.values() if c.priority in ("MEDIUM", "LOW"))


# -- broad keywords ---------------------------------------------------------------------

def test_fed_wording_alone_never_gives_strong_relevance(classified):
    assert classified[("Philly Fed Manufacturing Index", "High")].gold_relevance_level == "WEAK"
    assert classified[("Atlanta Fed GDPNow", "High")].gold_relevance_level == "NONE"
    assert classified[("Fed Bank Stress Test Results", "High")].gold_relevance_level == "NONE"
    rules = PriorityRules.from_file(RULES_PATH)
    for name in ["Fed", "Fed Speaks", "Fed Chair", "Federal Reserve Open House", "New York Fed Survey",
                 "Dallas Fed Manufacturing Index", "Kansas City Fed Index", "Fed Balance Sheet",
                 "FOMC", "FOMC Member", "Treasury Statement", "Bank Lending Survey", "Federal Holiday"]:
        c = rules.classify("x", "USD", name, "High")
        assert (c.gold_relevance_level, c.priority, c.highlight_required) == ("NONE", "LOW", False), name


def test_rule_file_contains_no_bare_keyword_patterns():
    """Every pattern must name an event; none may be a lone word wrapped in wildcards."""
    config = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    sections = [g["events"] for s in ("exclusions", "strong_gold_events", "moderate_gold_events", "weak_gold_events")
                for g in config[s]]
    sections += [config["critical_events"]["events"], config["high_priority_events"]["events"],
                 config["highlight"]["high_priority_events"]]
    for pattern in (p for events in sections for p in events):
        fixed_words = pattern.replace("*", " ").split()
        assert len(fixed_words) >= 2, f"'{pattern}' is too broad"
        assert not (pattern.startswith("*") and pattern.endswith("*")), f"'{pattern}' matches a substring"


# -- determinism and storage ------------------------------------------------------------

def test_reclassifying_the_synthetic_events_changes_nothing(classified, repo, settings):  # noqa: F811
    total = len(classified)
    for moment in ("2026-11-02T00:00:00Z", "2026-11-03T00:00:00Z"):
        result = classify_events(settings, repo, now=moment)
        assert (result.inserted, result.updated, result.unchanged) == (0, 0, total)
    assert repo.count_classifications() == total == repo.count()
    again = {(r.event.event_name, r.event.impact): r.classification for r in load_classified(repo)}
    for key, before in classified.items():
        after = again[key]
        assert after.updated_at == before.updated_at == NOW
        assert {k: v for k, v in after.to_dict().items() if k != "classified_at"} == {
            k: v for k, v in before.to_dict().items() if k != "classified_at"}
