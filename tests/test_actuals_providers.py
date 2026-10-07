"""Providers, with the network replaced. The BLS fixture is a real response saved on 2026-10-08;
the FRED payloads are hand-written in the documented response shape (no key was available to record one)."""

import io
import json
import os
import urllib.error
from datetime import date
from decimal import Decimal

import pytest

from src.actuals import providers
from src.actuals.mapping import ActualEventMapping
from src.actuals.providers import BLSProvider, FREDProvider, ProviderError, SeriesRequest, build_providers
from src.config import PROJECT_ROOT

BLS_FIXTURE = (PROJECT_ROOT / "fixtures" / "bls_timeseries_sample.json").read_text(encoding="utf-8")
KEY = "k3y-SECRET-0123456789abcdef"


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def patch_http(monkeypatch, body=None, error=None, calls=None):
    def urlopen(request, timeout=None):
        if calls is not None:
            calls.append(request)
        if error is not None:
            raise error
        return FakeResponse((body(request) if callable(body) else body).encode("utf-8"))
    monkeypatch.setattr(providers.urllib.request, "urlopen", urlopen)


def bls_requests():
    return [SeriesRequest("CUSR0000SA0", date(2026, 6, 1), date(2026, 8, 1)),
            SeriesRequest("CES0000000001", date(2025, 12, 1), date(2026, 9, 1)),
            SeriesRequest("LNS14000000", date(2026, 9, 1), date(2026, 9, 1))]


# -- BLS ------------------------------------------------------------------------------

def test_bls_parses_a_real_response(monkeypatch):
    calls = []
    patch_http(monkeypatch, BLS_FIXTURE, calls=calls)
    data = BLSProvider().fetch(bls_requests())

    assert data["CUSR0000SA0"][date(2026, 8, 1)] == Decimal("334.131")
    assert data["CES0000000001"][date(2026, 9, 1)] == Decimal("159044")
    assert data["LNS14000000"][date(2026, 9, 1)] == Decimal("4.2")
    assert date(2025, 10, 1) not in data["CUSR0000SA0"]  # published as "-" (no data collected that month)
    assert date(2025, 10, 1) in data["CES0000000001"]

    (request,) = calls  # every series in ONE request
    assert request.full_url == "https://api.bls.gov/publicAPI/v1/timeseries/data/"
    sent = json.loads(request.data)
    assert sent == {"seriesid": ["CES0000000001", "CUSR0000SA0", "LNS14000000"], "startyear": "2025", "endyear": "2026"}


def test_bls_fixture_gives_the_expected_actuals(monkeypatch):
    """Real published numbers through the real mapping: the values an enrichment run would store."""
    patch_http(monkeypatch, BLS_FIXTURE)
    mapping = ActualEventMapping.from_file(PROJECT_ROOT / "config" / "actual_event_mapping.json")
    data = BLSProvider().fetch(bls_requests())
    expected = {
        ("CPI m/m", date(2026, 9, 11)): "0.4%", ("CPI y/y", date(2026, 9, 11)): "3.4%",
        ("Core CPI m/m", date(2026, 9, 11)): "0.3%", ("PPI m/m", date(2026, 9, 10)): "0.4%",
        ("Core PPI m/m", date(2026, 9, 10)): "0.2%", ("Non-Farm Employment Change", date(2026, 10, 2)): "29K",
        ("Unemployment Rate", date(2026, 10, 2)): "4.2%", ("Average Hourly Earnings m/m", date(2026, 10, 2)): "0.1%",
        ("CPI m/m", date(2026, 10, 14)): None,   # September CPI is not in the fixture: not published yet
        ("CPI m/m", date(2025, 12, 10)): None,   # needs October 2025, which BLS never published
    }
    for (name, release), value in expected.items():
        m = mapping.find("USD", name)
        assert m.compute(data[m.series_id], m.period_for(release)) == value, (name, release)


def test_bls_uses_version_2_and_sends_the_key_only_in_the_body(monkeypatch):
    calls = []
    patch_http(monkeypatch, BLS_FIXTURE, calls=calls)
    BLSProvider(KEY).fetch(bls_requests())
    (request,) = calls
    assert request.full_url == "https://api.bls.gov/publicAPI/v2/timeseries/data/"
    assert KEY not in request.full_url
    assert json.loads(request.data)["registrationkey"] == KEY


def test_bls_needs_no_key_and_makes_no_request_for_nothing(monkeypatch):
    calls = []
    patch_http(monkeypatch, BLS_FIXTURE, calls=calls)
    assert BLSProvider().unavailable_reason() is None
    assert BLSProvider().fetch([]) == {} and calls == []


def test_bls_daily_limit_is_a_provider_error(monkeypatch):
    patch_http(monkeypatch, json.dumps({
        "status": "REQUEST_NOT_PROCESSED",
        "message": [f"daily threshold for total number of requests allocated to the user with registration key {KEY} has been reached."],
        "Results": {}}))
    with pytest.raises(ProviderError) as error:
        BLSProvider(KEY).fetch(bls_requests())
    assert "daily threshold" in str(error.value) and KEY not in str(error.value)


# -- FRED -----------------------------------------------------------------------------

def fred_body(request):
    series = request.full_url.split("series_id=")[1].split("&")[0]
    rows = {
        "ICSA": [{"date": "2026-09-26", "value": "210000"}, {"date": "2026-10-03", "value": "218000"}],
        "DFEDTARU": [{"date": "2026-10-28", "value": "4.00"}, {"date": "2026-10-29", "value": "."}],
    }[series]
    return json.dumps({"observation_start": "x", "count": len(rows), "observations": [
        {"realtime_start": "2026-10-08", "realtime_end": "2026-10-08", **row} for row in rows]})


def test_fred_requires_a_key():
    assert "FRED_API_KEY is not set" in FREDProvider().unavailable_reason()
    assert FREDProvider("  ").unavailable_reason() is not None
    assert FREDProvider(KEY).unavailable_reason() is None


def test_fred_parses_observations_and_skips_missing_values(monkeypatch):
    calls = []
    patch_http(monkeypatch, fred_body, calls=calls)
    data = FREDProvider(KEY).fetch([
        SeriesRequest("ICSA", date(2026, 10, 3), date(2026, 10, 3)),
        SeriesRequest("ICSA", date(2026, 9, 26), date(2026, 9, 26)),
        SeriesRequest("DFEDTARU", date(2026, 10, 29), date(2026, 10, 29)),
    ])
    assert data["ICSA"] == {date(2026, 9, 26): Decimal("210000"), date(2026, 10, 3): Decimal("218000")}
    assert data["DFEDTARU"] == {date(2026, 10, 28): Decimal("4.00")}  # "." means not published
    assert len(calls) == 2  # one per series, duplicates merged
    icsa = next(c.full_url for c in calls if "ICSA" in c.full_url)
    assert "observation_start=2026-09-26" in icsa and "observation_end=2026-10-03" in icsa and "file_type=json" in icsa


# -- failures and secrets ------------------------------------------------------------------

def http_error(code, body):
    return urllib.error.HTTPError("https://example.invalid/?api_key=" + KEY, code, "err", None, io.BytesIO(body.encode()))


@pytest.mark.parametrize("error, text", [
    (urllib.error.URLError("getaddrinfo failed"), "Could not reach FRED"),
    (TimeoutError("timed out"), "Could not reach FRED"),
    (http_error(400, json.dumps({"error_code": 400, "error_message": f"Bad Request. The value for variable api_key ({KEY}) is not registered."})), "HTTP 400"),
    (http_error(429, "Too Many Requests"), "HTTP 429"),
    (http_error(500, "<html>oops</html>"), "HTTP 500"),
])
def test_fred_failures_are_provider_errors_without_the_key(monkeypatch, error, text):
    patch_http(monkeypatch, error=error)
    with pytest.raises(ProviderError) as raised:
        FREDProvider(KEY).fetch([SeriesRequest("ICSA", date(2026, 10, 3), date(2026, 10, 3))])
    message = str(raised.value)
    assert text in message
    assert KEY not in message and "api_key" not in message.replace("variable api_key", "")


@pytest.mark.parametrize("body", ["not json", "[]", '{"error_message": "no"}'])
def test_unexpected_fred_responses_are_provider_errors(monkeypatch, body):
    patch_http(monkeypatch, body)
    with pytest.raises(ProviderError):
        FREDProvider(KEY).fetch([SeriesRequest("ICSA", date(2026, 10, 3), date(2026, 10, 3))])


@pytest.mark.parametrize("body", ["<html>blocked</html>", "[1, 2]", ""])
def test_unexpected_bls_responses_are_provider_errors(monkeypatch, body):
    patch_http(monkeypatch, body)
    with pytest.raises(ProviderError):
        BLSProvider().fetch(bls_requests())


def test_providers_are_built_from_settings(settings):
    from dataclasses import replace
    built = build_providers(replace(settings, fred_api_key=KEY))
    assert set(built) == {"BLS", "FRED"}
    assert built["FRED"].unavailable_reason() is None
    assert build_providers(settings)["FRED"].unavailable_reason() is not None


@pytest.mark.skipif(os.environ.get("RUN_LIVE_TESTS") != "1",
                    reason="live request to the BLS public API; set RUN_LIVE_TESTS=1 to enable")
def test_live_bls_returns_the_unemployment_rate():
    today = date.today()
    data = BLSProvider().fetch([SeriesRequest("LNS14000000", date(today.year - 1, 1, 1), today)])
    assert data["LNS14000000"] and all(0 < v < 30 for v in data["LNS14000000"].values())
