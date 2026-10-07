"""The rule engine: Gold relevance level, category, priority, highlight. No database."""

import copy
import json

import pytest

from src.classification.rules import PriorityRules, RulesConfigError
from src.config import PROJECT_ROOT

RULES_PATH = PROJECT_ROOT / "config" / "gold_priority_rules.json"


@pytest.fixture(scope="module")
def config():
    return json.loads(RULES_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def rules():
    return PriorityRules.from_file(RULES_PATH)


def classify(rules, name, impact="High", currency="USD"):
    return rules.classify("ff-test", currency, name, impact)


# -- Gold relevance level and category ------------------------------------------

@pytest.mark.parametrize("name, category", [
    ("CPI m/m", "INFLATION"), ("CPI y/y", "INFLATION"), ("Core CPI m/m", "INFLATION"),
    ("PPI m/m", "INFLATION"), ("Core PPI m/m", "INFLATION"), ("Core PCE Price Index m/m", "INFLATION"),
    ("Non-Farm Employment Change", "EMPLOYMENT"), ("Unemployment Rate", "EMPLOYMENT"),
    ("Average Hourly Earnings m/m", "EMPLOYMENT"),
    ("Federal Funds Rate", "MONETARY_POLICY"), ("FOMC Statement", "MONETARY_POLICY"),
    ("FOMC Press Conference", "MONETARY_POLICY"), ("FOMC Economic Projections", "MONETARY_POLICY"),
    ("Fed Chair Powell Speaks", "FED_COMMUNICATION"), ("Fed Chair Powell Testifies", "FED_COMMUNICATION"),
    ("FOMC Meeting Minutes", "FED_COMMUNICATION"),
    ("Advance GDP q/q", "GROWTH"), ("Prelim GDP q/q", "GROWTH"),
    ("Retail Sales m/m", "CONSUMER_ACTIVITY"), ("Core Retail Sales m/m", "CONSUMER_ACTIVITY"),
])
def test_strong_gold_events(rules, name, category):
    c = classify(rules, name)
    assert (c.gold_relevance_level, c.gold_relevance, c.category) == ("STRONG", True, category)


@pytest.mark.parametrize("name, category", [
    ("ADP Non-Farm Employment Change", "EMPLOYMENT"), ("Unemployment Claims", "EMPLOYMENT"),
    ("JOLTS Job Openings", "EMPLOYMENT"), ("Employment Cost Index q/q", "EMPLOYMENT"),
    ("Prelim UoM Inflation Expectations", "INFLATION"), ("Revised UoM Inflation Expectations", "INFLATION"),
    ("Advance GDP Price Index q/q", "INFLATION"),
    ("ISM Manufacturing PMI", "MANUFACTURING"), ("ISM Services PMI", "SERVICES"),
    ("CB Consumer Confidence", "SENTIMENT"), ("Prelim UoM Consumer Sentiment", "SENTIMENT"),
    ("Revised UoM Consumer Sentiment", "SENTIMENT"),
    ("FOMC Member Waller Speaks", "FED_COMMUNICATION"), ("Beige Book", "FED_COMMUNICATION"),
    ("Final GDP q/q", "GROWTH"), ("Durable Goods Orders m/m", "BUSINESS_ACTIVITY"),
    ("Personal Spending m/m", "CONSUMER_ACTIVITY"),
])
def test_moderate_gold_events(rules, name, category):
    c = classify(rules, name)
    assert (c.gold_relevance_level, c.gold_relevance, c.category) == ("MODERATE", True, category)


@pytest.mark.parametrize("name, category", [
    ("Philly Fed Manufacturing Index", "MANUFACTURING"), ("Empire State Manufacturing Index", "MANUFACTURING"),
    ("Richmond Manufacturing Index", "MANUFACTURING"), ("Flash Manufacturing PMI", "MANUFACTURING"),
    ("Chicago PMI", "MANUFACTURING"), ("Industrial Production m/m", "MANUFACTURING"),
    ("Final Services PMI", "SERVICES"), ("Flash Services PMI", "SERVICES"),
    ("Existing Home Sales", "HOUSING"), ("New Home Sales", "HOUSING"), ("Pending Home Sales m/m", "HOUSING"),
    ("ADP Weekly Employment Change", "EMPLOYMENT"), ("Challenger Job Cuts y/y", "EMPLOYMENT"),
    ("Import Prices m/m", "INFLATION"), ("Prelim Unit Labor Costs q/q", "INFLATION"),
    ("Personal Income m/m", "CONSUMER_ACTIVITY"),
    ("President Trump Speaks", "GOVERNMENT_FISCAL"), ("Treasury Sec Bessent Speaks", "GOVERNMENT_FISCAL"),
])
def test_weak_gold_events(rules, name, category):
    c = classify(rules, name)
    assert (c.gold_relevance_level, c.gold_relevance, c.category) == ("WEAK", True, category)


@pytest.mark.parametrize("name, category", [
    ("Atlanta Fed GDPNow", "GROWTH"), ("Fed Bank Stress Test Results", "OTHER"), ("Bank Holiday", "OTHER"),
    ("10-y Bond Auction", "GOVERNMENT_FISCAL"), ("30-y Bond Auction", "GOVERNMENT_FISCAL"),
    ("Federal Budget Balance", "GOVERNMENT_FISCAL"), ("Crude Oil Inventories", "ENERGY"),
    ("Natural Gas Storage", "ENERGY"), ("Trade Balance", "TRADE"), ("Consumer Credit m/m", "CONSUMER_ACTIVITY"),
    ("Final Wholesale Inventories m/m", "BUSINESS_ACTIVITY"), ("RCM/TIPP Economic Optimism", "SENTIMENT"),
])
def test_no_gold_relevance(rules, name, category):
    c = classify(rules, name)
    assert (c.gold_relevance_level, c.gold_relevance, c.category) == ("NONE", False, category)
    assert (c.priority, c.highlight_required) == ("LOW", False)


def test_generic_fed_keyword_does_not_create_strong_relevance(rules):
    # Each of these contains "Fed"/"Federal"/"Bank"/"Treasury" and was, or could be, caught by a bare keyword.
    levels = {name: classify(rules, name).gold_relevance_level for name in [
        "Philly Fed Manufacturing Index", "Atlanta Fed GDPNow", "Fed Bank Stress Test Results",
        "Federal Budget Balance", "Treasury Currency Report", "Bank Holiday",
        "Fed Something Nobody Has Defined", "Federal Open Day", "Treasury Refunding Announcement",
    ]}
    assert "STRONG" not in levels.values() and "MODERATE" not in levels.values()
    assert levels["Philly Fed Manufacturing Index"] == "WEAK"
    assert levels["Fed Something Nobody Has Defined"] == "NONE"


def test_matching_covers_the_whole_name_only(rules):
    assert classify(rules, "CPI m/m").gold_relevance_level == "STRONG"
    for near_miss in ["CPI", "Cleveland CPI m/m", "CPI m/m Flash Estimate", "Median CPI m/m", "Trimmed Mean CPI m/m"]:
        assert classify(rules, near_miss).gold_relevance_level == "NONE", near_miss


def test_matching_ignores_case_and_extra_spaces(rules):
    assert classify(rules, "  cpi  M/M ").gold_relevance_level == "STRONG"
    assert classify(rules, "fomc member WALLER speaks").category == "FED_COMMUNICATION"


# -- priority ---------------------------------------------------------------------

@pytest.mark.parametrize("name, impact, priority, score", [
    ("CPI m/m", "High", "CRITICAL", 100),
    ("Non-Farm Employment Change", "High", "CRITICAL", 100),
    ("Federal Funds Rate", "High", "CRITICAL", 100),
    ("Fed Chair Powell Speaks", "Medium", "CRITICAL", 90),
    ("CPI m/m", "Low", "HIGH", 80),
    ("FOMC Meeting Minutes", "High", "HIGH", 85),
    ("Retail Sales m/m", "High", "HIGH", 85),
    ("PPI m/m", "Medium", "HIGH", 75),
    ("Unemployment Claims", "High", "HIGH", 70),
    ("Unemployment Claims", "Medium", "MEDIUM", 60),
    ("ISM Services PMI", "Medium", "MEDIUM", 60),
    ("CB Consumer Confidence", "High", "MEDIUM", 55),
    ("Prelim UoM Consumer Sentiment", "Medium", "MEDIUM", 45),
    ("FOMC Member Waller Speaks", "Medium", "MEDIUM", 45),
    ("Philly Fed Manufacturing Index", "High", "MEDIUM", 40),
    ("FOMC Member Bowman Speaks", "Low", "LOW", 35),
    ("President Trump Speaks", "Medium", "LOW", 30),
    ("Final Services PMI", "Low", "LOW", 20),
    ("Crude Oil Inventories", "High", "LOW", 30),
    ("Bank Holiday", "Holiday", "LOW", 0),
])
def test_priority_and_score(rules, name, impact, priority, score):
    c = classify(rules, name, impact)
    assert (c.priority, c.priority_score) == (priority, score)


def test_high_forex_factory_impact_is_not_automatically_critical(rules):
    for name in ["Crude Oil Inventories", "10-y Bond Auction", "Philly Fed Manufacturing Index",
                 "CB Consumer Confidence", "Unemployment Claims", "Retail Sales m/m", "Some Brand New Event"]:
        c = classify(rules, name, "High")
        assert c.priority != "CRITICAL", name
        assert not (c.highlight_required and c.priority != "HIGH"), name


def test_same_event_ranks_higher_with_higher_impact_but_impact_alone_does_not_decide(rules):
    order = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    for name in ["CPI m/m", "Unemployment Claims", "FOMC Member Waller Speaks"]:
        ranks = [order.index(classify(rules, name, impact).priority) for impact in ("Low", "Medium", "High")]
        assert ranks == sorted(ranks)
    assert classify(rules, "CPI m/m", "Low").priority == "HIGH"  # Low impact, still important
    assert classify(rules, "Natural Gas Storage", "High").priority == "LOW"  # High impact, not Gold-relevant


def test_priority_is_capped_by_gold_relevance(config):
    custom = copy.deepcopy(config)
    custom["scoring"]["gold_relevance_points"]["WEAK"] = 70  # would otherwise reach CRITICAL
    c = PriorityRules(custom).classify("x", "USD", "Chicago PMI", "High")
    assert c.priority == "MEDIUM" and "Capped at MEDIUM" in c.classification_reason


# -- highlight --------------------------------------------------------------------

def test_highlight_follows_critical_priority(rules):
    assert classify(rules, "CPI m/m", "High").highlight_required
    assert classify(rules, "Federal Funds Rate", "High").highlight_required
    assert not classify(rules, "CPI m/m", "Low").highlight_required  # HIGH priority, not listed for highlight
    assert not classify(rules, "Retail Sales m/m", "High").highlight_required
    assert not classify(rules, "Unemployment Claims", "High").highlight_required


def test_highlight_for_an_explicitly_listed_high_priority_event(rules):
    minutes = classify(rules, "FOMC Meeting Minutes", "High")
    assert (minutes.priority, minutes.highlight_required) == ("HIGH", True)
    assert "explicit rule" in minutes.classification_reason
    # The listing applies only while the event is HIGH priority.
    unrated = classify(rules, "FOMC Meeting Minutes", "None")
    assert (unrated.priority, unrated.highlight_required) == ("MEDIUM", False)


def test_high_impact_alone_never_highlights(rules):
    for name in ["Crude Oil Inventories", "30-y Bond Auction", "Trade Balance", "Unknown Event"]:
        assert not classify(rules, name, "High").highlight_required


# -- determinism, reasons, edge cases ------------------------------------------------

def test_classification_is_deterministic(rules):
    again = PriorityRules.from_file(RULES_PATH)
    for name in ["CPI m/m", "FOMC Member Waller Speaks", "Trade Balance", "Unknown Event", ""]:
        for impact in ["High", "Medium", "Low", "Holiday", "None"]:
            assert classify(rules, name, impact) == classify(rules, name, impact) == classify(again, name, impact)


def test_every_result_uses_a_configured_category_and_valid_levels(rules, config, feed_text):
    names = {e["title"] for e in json.loads(feed_text)} | {"", "Unknown Event"}
    for name in names:
        c = classify(rules, name, "Medium")
        assert c.category in config["categories"]
        assert c.gold_relevance_level in ("STRONG", "MODERATE", "WEAK", "NONE")
        assert c.priority in ("CRITICAL", "HIGH", "MEDIUM", "LOW")
        assert 0 <= c.priority_score <= 100
        assert c.gold_relevance == (c.gold_relevance_level != "NONE")
        assert c.gold_relevance_reason and c.classification_reason


def test_reasons_explain_and_never_give_a_trade_direction(rules, config):
    cpi = classify(rules, "CPI m/m", "High")
    assert cpi.gold_relevance_reason == "Major US inflation release with direct USD and monetary-policy relevance."
    assert cpi.classification_reason == (
        "Priority CRITICAL: score 100/100 = Forex Factory impact High (30) + Gold relevance STRONG (40) "
        "+ critical event (30).")
    text = json.dumps(config).lower() + cpi.classification_reason.lower()
    for word in ["bullish", "bearish", "buy ", "sell ", "long gold", "short gold", "will rise", "will fall"]:
        assert word not in text, word


@pytest.mark.parametrize("name", [None, "", "   ", "An Event Forex Factory Adds Next Year", "🙂", "CPI"])
def test_missing_or_unknown_names_are_handled_safely(rules, name):
    c = classify(rules, name, "High")
    assert (c.gold_relevance_level, c.category, c.priority, c.highlight_required) == ("NONE", "OTHER", "LOW", False)
    assert c.gold_relevance_reason


@pytest.mark.parametrize("impact", [None, "", "Non-Economic", "something new"])
def test_missing_or_unknown_impact_scores_zero_impact_points(rules, impact):
    c = classify(rules, "CPI m/m", impact)
    assert (c.priority_score, c.priority) == (70, "HIGH")


def test_only_usd_events_are_gold_relevant(rules):
    c = classify(rules, "CPI m/m", "High", currency="EUR")
    assert (c.gold_relevance_level, c.priority, c.highlight_required) == ("NONE", "LOW", False)
    assert c.category == "INFLATION"
    assert "Only USD" in c.gold_relevance_reason
    assert classify(rules, "CPI m/m", "High", currency=None).gold_relevance_level == "NONE"


def test_version_comes_from_the_rule_file(rules, config):
    assert classify(rules, "CPI m/m").classification_version == config["classification_version"] == "1.0.0"


# -- configuration ---------------------------------------------------------------------

def test_exclusions_override_every_other_section(config):
    custom = copy.deepcopy(config)
    custom["strong_gold_events"].append({"category": "FED_COMMUNICATION", "reason": "Any Fed event.", "events": ["Fed *"]})
    r = PriorityRules(custom)
    assert r.classify("x", "USD", "Fed Vice Chair Speaks", "High").gold_relevance_level == "STRONG"
    assert r.classify("x", "USD", "Fed Bank Stress Test Results", "High").gold_relevance_level == "NONE"
    assert r.classify("x", "USD", "Fed Bank Stress Test Results", "High").priority == "LOW"


def test_rules_can_be_changed_without_touching_python(config):
    custom = copy.deepcopy(config)
    custom["classification_version"] = "2.0.0"
    custom["weak_gold_events"][0]["events"].remove("Philly Fed Manufacturing Index")
    custom["moderate_gold_events"].append(
        {"category": "MANUFACTURING", "reason": "Promoted.", "events": ["Philly Fed Manufacturing Index"]})
    custom["highlight"]["high_priority_events"].append("Retail Sales m/m")
    r = PriorityRules(custom)
    philly = r.classify("x", "USD", "Philly Fed Manufacturing Index", "High")
    assert (philly.gold_relevance_level, philly.gold_relevance_reason, philly.classification_version) == (
        "MODERATE", "Promoted.", "2.0.0")
    assert r.classify("x", "USD", "Retail Sales m/m", "High").highlight_required


def test_the_shipped_rule_file_has_no_overlapping_rules(config):
    """No listed event name may be claimed by a second rule, so rule order never matters by accident."""
    r = PriorityRules(config)
    for section in ("exclusions", "strong_gold_events", "moderate_gold_events", "weak_gold_events"):
        for group in config[section]:
            for pattern in group["events"]:
                sample = pattern.replace("*", "Sample").replace("?", "m")
                hits = [rule.pattern for rule in r._rules if rule.regex.match(" ".join(sample.split()).casefold())]
                assert hits == [pattern], (pattern, hits)


def test_priority_lists_only_name_events_that_have_a_relevance_rule(config):
    r = PriorityRules(config)
    for section in ("critical_events", "high_priority_events"):
        for pattern in config[section]["events"]:
            sample = pattern.replace("*", "Sample").replace("?", "m")
            assert r.match(sample) is not None and r.match(sample).level != "NONE", pattern
    for pattern in config["critical_events"]["events"]:
        assert r.match(pattern.replace("*", "Sample").replace("?", "m")).level == "STRONG", pattern


@pytest.mark.parametrize("mutate, message", [
    (lambda c: c.pop("classification_version"), "malformed"),
    (lambda c: c.update(classification_version="  "), "must not be empty"),
    (lambda c: c["strong_gold_events"][0].update(category="CRYPTO"), "Unknown category 'CRYPTO'"),
    (lambda c: c["weak_gold_events"][0]["events"].append("core cpi  ?/?"), "listed in both"),
    (lambda c: c["weak_gold_events"][0]["events"].append(""), "empty or non-text"),
    (lambda c: c["scoring"]["priority_thresholds"].update(HIGH=95), "must decrease"),
    (lambda c: c["scoring"]["gold_relevance_points"].pop("WEAK"), "malformed"),
    (lambda c: c["highlight"].update(priorities=["URGENT"]), "unknown priority"),
    (lambda c: c["scoring"]["max_priority_by_gold_relevance"].update(WEAK="SOMETIMES"), "Invalid max_priority"),
])
def test_broken_rule_files_are_rejected_with_a_clear_message(config, mutate, message):
    custom = copy.deepcopy(config)
    mutate(custom)
    with pytest.raises(RulesConfigError, match=message):
        PriorityRules(custom)


def test_unreadable_rule_file(tmp_path):
    with pytest.raises(RulesConfigError, match="Cannot read"):
        PriorityRules.from_file(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(RulesConfigError, match="not valid JSON"):
        PriorityRules.from_file(bad)
