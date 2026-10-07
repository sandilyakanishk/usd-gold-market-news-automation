"""The actual-results view on the command line, and proof that the earlier views are untouched."""

import json
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from src import main as cli
from src.actuals import service
from src.collector import forex_factory
from src.database.database import SQLiteRepository

from .test_actuals_service import FixtureProvider
from .test_retrieval import fake_urlopen


@pytest.fixture
def sources():
    """Fixture week: Unemployment Claims (8 Oct) is the only event with a mapped source."""
    return {"BLS": FixtureProvider("BLS"),
            "FRED": FixtureProvider("FRED", {"ICSA": {date(2026, 10, 3): Decimal("218000")}})}


@pytest.fixture
def run(monkeypatch, settings, feed_text, capsys, sources):
    """CLI against the fixture feed. 'Now' is 9 Oct 2026 12:00 India time unless a test moves it."""
    state = {"settings": settings, "now": (2026, 10, 9, 12, 0)}
    monkeypatch.setattr(cli.Settings, "from_env", classmethod(lambda cls: state["settings"]))
    monkeypatch.setattr(forex_factory.urllib.request, "urlopen", fake_urlopen(feed_text))
    monkeypatch.setattr(service, "build_providers", lambda settings: sources)

    class Clock(cli.datetime):
        @classmethod
        def now(cls, tz=None):
            from zoneinfo import ZoneInfo
            return cls(*state["now"], tzinfo=ZoneInfo("Asia/Kolkata")).astimezone(tz)

    monkeypatch.setattr(cli, "datetime", Clock)
    monkeypatch.setattr(service, "datetime", Clock)

    def _run(*argv, now=None, **overrides):
        if now:
            state["now"] = now
        state["settings"] = replace(settings, **overrides)
        code = cli.main(list(argv))
        return code, capsys.readouterr()
    return _run


def test_enrich_writes_the_released_value_and_reports_it(run, settings):
    code, out = run("--enrich-actuals", "--week", "--released")
    assert code == 0
    assert out.out == """USD / GOLD ACTUAL RESULTS
=========================

2026-10-08 18:00 (Asia/Kolkata)

USD
Unemployment Claims

Forex Factory Impact: MEDIUM
Priority: MEDIUM
Previous: 197K
Forecast: 200K
Actual: 218K
Release Status: RELEASED
Actual Source: FRED (ICSA: Initial jobless claims, seasonally adjusted (Department of Labor), in thousands, period 2026-10-03)
Surprise: ABOVE_FORECAST (+18)

=========================
1 event(s)
Release status: UPCOMING 0 | RELEASED 1 | NO_DATA 0 | FAILED 0
This run: 23 record(s) were written (1 with a new or revised Actual), 0 unchanged. Source requests: FRED 1.
"""
    with SQLiteRepository(settings.database_path) as db:
        (claims,) = [e for e in db.query_events() if e.event_name == "Unemployment Claims"]
        assert (claims.actual, claims.forecast, claims.previous) == ("218K", "200K", "197K")
        assert db.count() == 23 == db.count_actual_records()


def test_week_status_summary(run):
    _, out = run("--enrich-actuals", "--week")
    # On 9 Oct 12:00 IST the three events of 9/10 Oct are still ahead.
    assert "Release status: UPCOMING 3 | RELEASED 1 | NO_DATA 19 | FAILED 0" in out.out
    assert "Note: This kind of event has no numeric result." in out.out
    assert "Note: No verified free source is mapped for this event." in out.out


def test_report_contains_no_trading_language(run):
    _, out = run("--enrich-actuals", "--week")
    text = out.out.lower()
    for word in ["bullish", "bearish", "buy", "sell", "long gold", "short gold", "target", "stop loss", "will rise", "will fall"]:
        assert word not in text, word


def test_running_again_changes_nothing(run, settings, sources):
    run("--enrich-actuals", "--week")
    for _ in range(2):
        _, out = run("--enrich-actuals", "--week")
        assert "This run: 0 record(s) were written (0 with a new or revised Actual), 23 unchanged. Source requests: none." in out.out
    assert len(sources["FRED"].requests) == 1
    with SQLiteRepository(settings.database_path) as db:
        assert db.count() == 23 == db.count_actual_records() == db.count_classifications()


def test_upcoming_event_is_not_looked_up_before_its_release(run, sources):
    _, out = run("--enrich-actuals", "--week", "--json", now=(2026, 10, 8, 17, 59))  # one minute before 18:00 IST
    claims = next(r for r in json.loads(out.out) if r["event_name"] == "Unemployment Claims")
    assert (claims["actual"], claims["actual_result"]["release_status"]) == (None, "UPCOMING")
    assert sources["FRED"].requests == []
    _, out = run("--enrich-actuals", "--week", "--json", now=(2026, 10, 8, 18, 0))
    claims = next(r for r in json.loads(out.out) if r["event_name"] == "Unemployment Claims")
    assert (claims["actual"], claims["actual_result"]["release_status"]) == ("218K", "RELEASED")


def test_dry_run_writes_nothing_and_does_not_sync(run, settings):
    run("--week")  # populate the database first
    code, out = run("--enrich-actuals", "--week", "--dry-run", "--released")
    assert code == 0
    assert "DRY RUN: nothing was written to the database." in out.out
    assert "Actual: 218K" in out.out
    assert "23 record(s) would be written (1 with a new or revised Actual)" in out.out
    with SQLiteRepository(settings.database_path) as db:
        assert db.count_actual_records() == 0 and db.count_classifications() == 0
        assert all(e.actual is None for e in db.query_events())


@pytest.mark.parametrize("argv", [["--dry-run"], ["--week", "--dry-run"], ["--recheck-released"], ["--actuals", "--dry-run"]])
def test_dry_run_and_recheck_need_enrich(run, argv):
    code, out = run(*argv)
    assert code == 2 and "only apply together with --enrich-actuals" in out.err


@pytest.mark.parametrize("argv, count", [
    (["--enrich-actuals"], 23),
    (["--enrich-actuals", "--today"], 2),
    (["--enrich-actuals", "--released"], 1),
    (["--enrich-actuals", "--missing-actual"], 19),
    (["--enrich-actuals", "--released", "--missing-actual"], 20),
    (["--enrich-actuals", "--medium-priority"], 5),
    (["--enrich-actuals", "--high-priority"], 1),
    (["--enrich-actuals", "--critical"], 0),
    (["--enrich-actuals", "--medium-priority", "--released"], 1),
    (["--enrich-actuals", "--gold"], 14),
    (["--enrich-actuals", "--high-impact"], 1),
    (["--enrich-actuals", "--date", "2026-10-08"], 7),
])
def test_actuals_view_filters(run, argv, count):
    code, out = run(*argv, "--json")
    assert code == 0
    assert len(json.loads(out.out)) == count


def test_priority_filter_limits_which_events_are_looked_up(run, settings, sources):
    run("--enrich-actuals", "--high-priority")  # only FOMC Meeting Minutes, which has no numeric result
    assert sources["FRED"].requests == []
    with SQLiteRepository(settings.database_path) as db:
        assert db.count_actual_records() == 1
    run("--enrich-actuals", "--medium-priority")  # includes Unemployment Claims
    assert len(sources["FRED"].requests) == 1


def test_stored_view_never_contacts_a_source(run, sources):
    _, before = run("--actuals", "--json")
    assert sources["FRED"].requests == []
    statuses = {r["actual_result"]["release_status"] for r in json.loads(before.out)}
    assert statuses == {"UPCOMING", "NO_DATA"}  # nothing checked yet
    run("--enrich-actuals")
    _, after = run("--released", "--json")
    (claims,) = json.loads(after.out)
    assert claims["actual"] == "218K"
    assert len(sources["FRED"].requests) == 1


def test_json_shape(run):
    _, out = run("--enrich-actuals", "--released", "--json")
    (row,) = json.loads(out.out)
    assert row["event_name"] == "Unemployment Claims" and row["source"] == "Forex Factory"
    assert row["actual_result"] == {
        "event_id": row["event_id"], "release_status": "RELEASED", "status_reason": "", "actual_source": "FRED",
        "actual_source_event": "ICSA: Initial jobless claims, seasonally adjusted (Department of Labor), in thousands",
        "actual_period": "2026-10-03", "actual_revision": 1, "surprise_status": "ABOVE_FORECAST", "surprise_value": 18.0,
        "actual_retrieved_at": "2026-10-09T06:30:00Z", "actual_updated_at": "2026-10-09T06:30:00Z",
        "updated_at": "2026-10-09T06:30:00Z",
    }
    assert row["classification"]["priority"] == "MEDIUM"


def test_missing_fred_key_is_reported_not_failed(run, sources):
    sources["FRED"].unavailable = "FRED_API_KEY is not set; this event's source needs that free key."
    _, out = run("--enrich-actuals", "--missing-actual")
    assert "Note: FRED_API_KEY is not set; this event's source needs that free key." in out.out
    assert "Release status: UPCOMING 0 | RELEASED 0 | NO_DATA 20 | FAILED 0" in out.out


def test_source_outage_is_failed(run, sources):
    sources["FRED"].error = "Could not reach FRED: timed out"
    code, out = run("--enrich-actuals", "--missing-actual")
    assert code == 0
    assert "Release Status: FAILED" in out.out and "Note: Could not reach FRED: timed out" in out.out
    assert "NO_DATA 19 | FAILED 1" in out.out


def test_earlier_views_are_unchanged_apart_from_the_actual_line(run):
    _, step1_before = run("--week")
    _, step2_before = run("--classify", "--week")
    run("--enrich-actuals", "--week")
    _, step1_after = run("--week")
    _, step2_after = run("--classify", "--week")
    assert step2_after.out == step2_before.out
    before, after = step1_before.out.splitlines(), step1_after.out.splitlines()
    changed = [(b, a) for b, a in zip(before, after) if b != a]
    assert len(before) == len(after) and changed == [("Actual: -", "Actual: 218K")]
    _, legacy = run("--week", "--gold", "--json")
    assert len(json.loads(legacy.out)) == 13 and "actual_result" not in json.loads(legacy.out)[0]


def test_broken_mapping_file_is_a_configuration_error(run, tmp_path):
    bad = tmp_path / "mapping.json"
    bad.write_text("{not json", encoding="utf-8")
    code, out = run("--enrich-actuals", actual_mapping_path=bad)
    assert code == 2 and out.err.startswith("CONFIGURATION ERROR: The actual-value mapping file")
    code, out = run("--week", actual_mapping_path=bad)  # earlier steps do not depend on it
    assert code == 0 and "23 event(s)" in out.out
