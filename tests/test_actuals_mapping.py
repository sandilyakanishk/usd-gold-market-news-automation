"""Event-to-series mapping, reference periods, value formatting and the surprise comparison."""

import copy
import json
from datetime import date
from decimal import Decimal

import pytest

from src.actuals.mapping import ActualEventMapping, MappingConfigError
from src.actuals.surprise import compare, parse_value
from src.config import PROJECT_ROOT

MAPPING_PATH = PROJECT_ROOT / "config" / "actual_event_mapping.json"


@pytest.fixture(scope="module")
def config():
    return json.loads(MAPPING_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def mapping():
    return ActualEventMapping.from_file(MAPPING_PATH)


# -- matching ---------------------------------------------------------------------

@pytest.mark.parametrize("name, provider, series", [
    ("CPI m/m", "BLS", "CUSR0000SA0"), ("CPI y/y", "BLS", "CUUR0000SA0"), ("Core CPI m/m", "BLS", "CUSR0000SA0L1E"),
    ("PPI m/m", "BLS", "WPSFD4"), ("Core PPI m/m", "BLS", "WPSFD49104"),
    ("Non-Farm Employment Change", "BLS", "CES0000000001"), ("Unemployment Rate", "BLS", "LNS14000000"),
    ("Average Hourly Earnings m/m", "BLS", "CES0500000003"),
    ("Core PCE Price Index m/m", "FRED", "PCEPILFE"), ("PCE Price Index m/m", "FRED", "PCEPI"),
    ("Advance GDP q/q", "FRED", "A191RL1Q225SBEA"), ("Prelim GDP q/q", "FRED", "A191RL1Q225SBEA"),
    ("Final GDP q/q", "FRED", "A191RL1Q225SBEA"),
    ("Retail Sales m/m", "FRED", "RSAFS"), ("Core Retail Sales m/m", "FRED", "RSFSXMV"),
    ("Unemployment Claims", "FRED", "ICSA"), ("Federal Funds Rate", "FRED", "DFEDTARU"),
])
def test_priority_usd_events_are_mapped(mapping, name, provider, series):
    found = mapping.find("USD", name)
    assert (found.provider, found.series_id) == (provider, series)
    assert mapping.find("usd", f"  {name.upper()}  ") is found  # case and spacing do not matter


@pytest.mark.parametrize("name", [
    "CPI", "Core CPI", "CPI q/q", "Median CPI m/m", "Cleveland CPI m/m", "CPI m/m Flash", "Trimmed Mean CPI m/m",
    "Core CPI y/y", "PPI y/y", "German Prelim CPI m/m", "Unemployment", "Unemployment Rate Forecast",
    "Non-Farm", "ADP Non-Farm Employment Change", "ADP Weekly Employment Change", "Continuing Claims",
    "GDP", "GDP Price Index q/q", "Advance GDP Price Index q/q", "Retail Sales", "Retail Sales y/y",
    "FOMC Statement", "Federal Funds Rate Decision", "ISM Services PMI", "", None,
])
def test_similar_looking_names_are_not_matched(mapping, name):
    assert mapping.find("USD", name) is None


@pytest.mark.parametrize("currency", ["EUR", "GBP", "CAD", "JPY", "All", "", None])
def test_only_usd_events_are_matched(mapping, currency):
    for name in ("CPI m/m", "Unemployment Rate", "Retail Sales m/m", "Core CPI m/m"):
        assert mapping.find(currency, name) is None


def test_events_without_a_numeric_result(mapping):
    for name in ["FOMC Member Waller Speaks", "Fed Chair Powell Speaks", "Fed Chair Powell Testifies", "FOMC Statement",
                 "FOMC Press Conference", "FOMC Meeting Minutes", "President Trump Speaks", "Bank Holiday"]:
        assert mapping.has_no_actual(name) and mapping.find("USD", name) is None
    for name in ["CPI m/m", "Unemployment Claims", "ISM Services PMI", "Federal Funds Rate", "", None]:
        assert not mapping.has_no_actual(name)


def test_only_official_public_series_are_mapped(mapping, config):
    assert {m.provider for m in mapping.mappings} == {"BLS", "FRED"}
    assert all(config["providers"][m.provider]["official"] for m in mapping.mappings)
    # ADP's data is copyrighted by a third party on FRED, so it is deliberately not mapped.
    assert not any("ADP" in m.series_id for m in mapping.mappings)


# -- reference period ----------------------------------------------------------------

@pytest.mark.parametrize("name, release, period, label", [
    ("CPI m/m", date(2026, 10, 14), date(2026, 9, 1), "2026-09"),
    ("CPI m/m", date(2027, 1, 13), date(2026, 12, 1), "2026-12"),
    ("Non-Farm Employment Change", date(2026, 10, 2), date(2026, 9, 1), "2026-09"),
    ("Core PCE Price Index m/m", date(2026, 10, 30), date(2026, 9, 1), "2026-09"),
    ("Unemployment Claims", date(2026, 10, 8), date(2026, 10, 3), "2026-10-03"),   # Thursday -> prior Saturday
    ("Unemployment Claims", date(2026, 10, 7), date(2026, 10, 3), "2026-10-03"),   # holiday week, Wednesday
    ("Unemployment Claims", date(2026, 10, 10), date(2026, 10, 3), "2026-10-03"),  # a Saturday -> the one before
    ("Advance GDP q/q", date(2026, 10, 29), date(2026, 7, 1), "2026-Q3"),
    ("Prelim GDP q/q", date(2026, 11, 25), date(2026, 7, 1), "2026-Q3"),
    ("Final GDP q/q", date(2026, 12, 22), date(2026, 7, 1), "2026-Q3"),
    ("Advance GDP q/q", date(2027, 1, 28), date(2026, 10, 1), "2026-Q4"),
    ("Federal Funds Rate", date(2026, 10, 28), date(2026, 10, 29), "2026-10-29"),
])
def test_reference_period(mapping, name, release, period, label):
    found = mapping.find("USD", name)
    assert found.period_for(release) == period
    assert found.period_label(period) == label


# -- value computation ---------------------------------------------------------------

def obs(**values):
    return {date.fromisoformat(k[1:].replace("_", "-")): Decimal(v) for k, v in values.items()}


def test_percent_change_is_rounded_like_the_published_figure(mapping):
    cpi = mapping.find("USD", "CPI m/m")
    assert cpi.compute(obs(d2026_08_01="332.813", d2026_09_01="334.131"), date(2026, 9, 1)) == "0.4%"
    assert cpi.compute(obs(d2026_08_01="300.000", d2026_09_01="300.450"), date(2026, 9, 1)) == "0.2%"  # 0.15 rounds up
    assert cpi.compute(obs(d2026_08_01="300.000", d2026_09_01="299.400"), date(2026, 9, 1)) == "-0.2%"
    assert cpi.compute(obs(d2026_08_01="300.000", d2026_09_01="299.990"), date(2026, 9, 1)) == "0.0%"  # never "-0.0%"


def test_twelve_month_change_uses_the_same_month_a_year_earlier(mapping):
    yoy = mapping.find("USD", "CPI y/y")
    data = obs(d2025_09_01="320.000", d2026_08_01="329.000", d2026_09_01="329.600")
    assert yoy.compute(data, date(2026, 9, 1)) == "3.0%"


def test_change_level_and_scaled_values(mapping):
    nfp = mapping.find("USD", "Non-Farm Employment Change")
    assert nfp.compute(obs(d2026_08_01="159015", d2026_09_01="159044"), date(2026, 9, 1)) == "29K"
    assert nfp.compute(obs(d2026_08_01="159015", d2026_09_01="158940"), date(2026, 9, 1)) == "-75K"
    assert mapping.find("USD", "Unemployment Rate").compute(obs(d2026_09_01="4.2"), date(2026, 9, 1)) == "4.2%"
    assert mapping.find("USD", "Unemployment Claims").compute(obs(d2026_10_03="218000"), date(2026, 10, 3)) == "218K"
    assert mapping.find("USD", "Federal Funds Rate").compute(obs(d2026_10_29="4.0"), date(2026, 10, 29)) == "4.00%"
    assert mapping.find("USD", "Advance GDP q/q").compute(obs(d2026_07_01="-0.3"), date(2026, 7, 1)) == "-0.3%"


def test_no_value_is_produced_when_the_period_or_its_base_is_missing(mapping):
    cpi = mapping.find("USD", "CPI m/m")
    assert cpi.compute({}, date(2026, 9, 1)) is None
    assert cpi.compute(obs(d2026_08_01="332.813"), date(2026, 9, 1)) is None            # not published yet
    assert cpi.compute(obs(d2026_09_01="334.131"), date(2026, 9, 1)) is None            # previous month missing
    assert cpi.compute(obs(d2026_07_01="330.0", d2026_09_01="334.131"), date(2026, 9, 1)) is None  # never skips a month
    assert mapping.find("USD", "Unemployment Claims").compute(obs(d2026_09_26="210000"), date(2026, 10, 3)) is None


# -- surprise ------------------------------------------------------------------------

@pytest.mark.parametrize("actual, forecast, status, value", [
    ("3.2%", "3.0%", "ABOVE_FORECAST", 0.2),
    ("0.2%", "0.3%", "BELOW_FORECAST", -0.1),
    ("0.3%", "0.3%", "IN_LINE_WITH_FORECAST", 0.0),
    ("0.30%", "0.3%", "IN_LINE_WITH_FORECAST", 0.0),
    ("218K", "200K", "ABOVE_FORECAST", 18.0),
    ("29K", "50K", "BELOW_FORECAST", -21.0),
    ("-75K", "50K", "BELOW_FORECAST", -125.0),
    ("-100.8B", "-88.6B", "BELOW_FORECAST", -12.2),
    ("4.50%", "4.25%", "ABOVE_FORECAST", 0.25),
    ("1.2M", "900K", "ABOVE_FORECAST", None),   # direction is certain, the units differ
    ("900K", "1.2M", "BELOW_FORECAST", None),
    ("1,250", "1,200", "ABOVE_FORECAST", 50.0),
])
def test_surprise(actual, forecast, status, value):
    assert compare(actual, forecast) == (status, value)


@pytest.mark.parametrize("actual, forecast", [
    ("3.2%", None), ("3.2%", ""), (None, "3.0%"), (None, None),
    ("4.83|2.7", "4.80|2.6"), ("3.2%", "<3.0%"), ("3.2%", "about 3"), ("3.2%", "3.0"), ("200K", "0.2%"), ("n/a", "0.3%"),
])
def test_surprise_is_not_guessed(actual, forecast):
    assert compare(actual, forecast) == ("NOT_AVAILABLE", None)


def test_parse_value():
    assert parse_value("0.3%") == (Decimal("0.3"), "%")
    assert parse_value(" -100.8b ") == (Decimal("-100.8"), "B")
    assert parse_value("254K") == (Decimal("254"), "K")
    assert parse_value("47.5") == (Decimal("47.5"), "")
    assert parse_value("4.83|2.7") is None and parse_value("") is None and parse_value(None) is None


# -- configuration -------------------------------------------------------------------

@pytest.mark.parametrize("mutate, message", [
    (lambda c: c.pop("mappings"), "malformed"),
    (lambda c: c["mappings"][0].update(provider="Bloomberg"), "unknown provider"),
    (lambda c: c["mappings"][0].update(transform="guess"), "unknown transform"),
    (lambda c: c["mappings"][0].update(reference_period="previous_quarter"), "does not fit frequency"),
    (lambda c: c["mappings"][0].update(forex_factory_events=["CPI *"]), "without wildcards"),
    (lambda c: c["mappings"][1]["forex_factory_events"].append("cpi M/M"), "mapped more than once"),
    (lambda c: c["mappings"][0]["forex_factory_events"].append("FOMC Statement"), "events_without_actual"),
    (lambda c: c["mappings"][10].update(transform="percent_change"), "needs a monthly series"),
    (lambda c: c["mappings"][0].update(forex_factory_events=[]), "lists no Forex Factory event"),
])
def test_broken_mapping_files_are_rejected(config, mutate, message):
    custom = copy.deepcopy(config)
    mutate(custom)
    with pytest.raises(MappingConfigError, match=message):
        ActualEventMapping(custom)


def test_unreadable_mapping_file(tmp_path):
    with pytest.raises(MappingConfigError, match="Cannot read"):
        ActualEventMapping.from_file(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("[]", encoding="utf-8")
    with pytest.raises(MappingConfigError, match="JSON object"):
        ActualEventMapping.from_file(bad)
