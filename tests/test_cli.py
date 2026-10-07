import json
from datetime import date

import pytest

from src import main as cli
from src.collector import forex_factory

from .test_retrieval import fake_urlopen


@pytest.fixture
def run(monkeypatch, settings, feed_text, capsys):
    """Run the CLI against the fixture feed, with 'today' pinned to 2026-10-08 (India)."""
    monkeypatch.setattr(cli.Settings, "from_env", classmethod(lambda cls: settings))
    monkeypatch.setattr(forex_factory.urllib.request, "urlopen", fake_urlopen(feed_text))

    class FixedDateTime(cli.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 8, 12, 0, tzinfo=tz)

    monkeypatch.setattr(cli, "datetime", FixedDateTime)

    def _run(*argv):
        code = cli.main(list(argv))
        return code, capsys.readouterr()
    return _run


def test_today_text_output(run):
    code, out = run("--today")
    assert code == 0
    assert out.out.startswith("FOREX FACTORY — USD / GOLD EVENTS\n=====")
    block = ("2026-10-08 18:00 (Asia/Kolkata)\nUSD\nUnemployment Claims\nImpact: MEDIUM\n"
             "Gold Relevance: YES\nForecast: 200K\nPrevious: 197K\nActual: -")
    assert block in out.out
    assert "7 event(s), 0 high impact" in out.out
    assert "EUR" not in out.out


def test_json_output_has_the_documented_fields(run):
    _, out = run("--week", "--gold-high-impact", "--json")
    (event,) = json.loads(out.out)
    assert event["event_name"] == "FOMC Meeting Minutes"
    assert (event["impact"], event["gold_relevance"], event["actual"]) == ("High", True, None)
    assert set(event) == {
        "event_id", "date", "time", "timezone", "datetime_utc", "currency", "event_name", "impact",
        "original_impact", "gold_relevance", "forecast", "previous", "actual", "source", "source_url",
        "retrieved_at", "updated_at",
    }


@pytest.mark.parametrize("argv, count", [
    (["--week"], 23),
    (["--week", "--usd"], 23),
    (["--week", "--high-impact"], 1),
    (["--week", "--medium-impact"], 6),
    (["--week", "--gold"], 13),
    (["--week", "--gold-medium-impact"], 5),
    (["--week", "--high-impact", "--medium-impact"], 7),
    (["--date", "2026-10-07"], 5),
    (["--from", "2026-10-06", "--to", "2026-10-07"], 11),
    (["--tomorrow"], 2),
    (["--date", "2031-01-01"], 0),
])
def test_filters(run, argv, count):
    _, out = run(*argv, "--json")
    assert len(json.loads(out.out)) == count


def test_invalid_date_is_rejected(run):
    code, out = run("--date", "08-10-2026")
    assert code == 2 and "Invalid date" in out.err


def test_no_fetch_reads_only_the_database(run, monkeypatch):
    run("--week")
    monkeypatch.setattr(forex_factory.urllib.request, "urlopen", fake_urlopen(error=AssertionError("no network")))
    _, out = run("--week", "--no-fetch", "--json")
    assert len(json.loads(out.out)) == 23


def test_network_failure_on_first_run_reports_and_exits_cleanly(run, monkeypatch):
    import urllib.error
    monkeypatch.setattr(forex_factory.urllib.request, "urlopen",
                        fake_urlopen(error=urllib.error.URLError("offline")))
    code, out = run("--today")
    assert code == 0
    assert "could not refresh from Forex Factory" in out.err
    assert "No matching events." in out.out


def test_week_is_sunday_to_saturday():
    args = cli.build_parser().parse_args(["--week"])
    assert cli.resolve_dates(args, date(2026, 10, 8)) == ("2026-10-04", "2026-10-10")
    assert cli.resolve_dates(args, date(2026, 10, 4)) == ("2026-10-04", "2026-10-10")
    assert cli.resolve_dates(args, date(2026, 10, 10)) == ("2026-10-04", "2026-10-10")
