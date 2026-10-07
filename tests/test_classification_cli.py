"""The priority view on the command line, and proof that the Step 1 view is untouched."""

import json
from dataclasses import replace

import pytest

from src import main as cli
from src.collector import forex_factory
from src.database.database import SQLiteRepository

from .test_retrieval import fake_urlopen


@pytest.fixture
def run(monkeypatch, settings, feed_text, capsys):
    state = {"settings": settings}
    monkeypatch.setattr(cli.Settings, "from_env", classmethod(lambda cls: state["settings"]))
    monkeypatch.setattr(forex_factory.urllib.request, "urlopen", fake_urlopen(feed_text))

    class FixedDateTime(cli.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 8, 12, 0, tzinfo=tz)

    monkeypatch.setattr(cli, "datetime", FixedDateTime)

    def _run(*argv, **overrides):
        state["settings"] = replace(settings, **overrides)
        code = cli.main(list(argv))
        return code, capsys.readouterr()
    return _run


def test_priority_report_text(run):
    code, out = run("--classify", "--week", "--high-priority")
    assert code == 0
    assert out.out == """USD / GOLD EVENT PRIORITY
=========================

2026-10-07 23:30 (Asia/Kolkata)

USD
FOMC Meeting Minutes

Forex Factory Impact: HIGH
Gold Relevance: STRONG
Category: FED_COMMUNICATION
Priority: HIGH
Highlight: YES

Reason:
Fed Chair or full-committee communication that can shift interest-rate expectations.
Priority HIGH: score 85/100 = Forex Factory impact High (30) + Gold relevance STRONG (40) + high-priority event (15). Highlighted by an explicit rule for this event.

=========================
1 event(s), 1 Gold-relevant
Gold relevance: STRONG 1 | MODERATE 0 | WEAK 0 | NONE 0
Priority: CRITICAL 0 | HIGH 1 | MEDIUM 0 | LOW 0
Highlight: 1
Rules version: 1.0.0
"""


def test_week_summary(run):
    _, out = run("--classify", "--week")
    assert "23 event(s), 14 Gold-relevant" in out.out
    assert "Gold relevance: STRONG 1 | MODERATE 9 | WEAK 4 | NONE 9" in out.out
    assert "Priority: CRITICAL 0 | HIGH 1 | MEDIUM 5 | LOW 17" in out.out
    assert "Highlight: 1" in out.out
    assert "No rule yet for" not in out.out


def test_report_contains_no_trading_language(run):
    _, out = run("--classify", "--week")
    text = out.out.lower()
    for word in ["bullish", "bearish", "buy", "sell", "long gold", "short gold", "target", "stop loss"]:
        assert word not in text, word


@pytest.mark.parametrize("argv, count", [
    (["--classify"], 23),
    (["--classify", "--week"], 23),
    (["--classify", "--today"], 7),
    (["--critical"], 0),
    (["--high-priority"], 1),
    (["--medium-priority"], 5),
    (["--low-priority"], 17),
    (["--critical", "--high-priority"], 1),
    (["--high-priority", "--medium-priority"], 6),
    (["--highlight"], 1),
    (["--classify", "--gold"], 14),
    (["--classify", "--gold", "--today"], 3),
    (["--classify", "--gold-high-impact"], 1),
    (["--classify", "--gold-medium-impact"], 6),
    (["--classify", "--high-impact"], 1),
    (["--classify", "--medium-impact"], 6),
    (["--classify", "--date", "2026-10-07"], 5),
    (["--highlight", "--from", "2026-10-08", "--to", "2026-10-10"], 0),
])
def test_priority_view_filters(run, argv, count):
    code, out = run(*argv, "--json")
    assert code == 0
    assert len(json.loads(out.out)) == count


def test_json_carries_the_event_and_its_classification(run):
    _, out = run("--highlight", "--json")
    (row,) = json.loads(out.out)
    assert (row["event_name"], row["impact"], row["original_impact"], row["actual"]) == (
        "FOMC Meeting Minutes", "High", "High", None)
    assert set(row["classification"]) == {
        "event_id", "gold_relevance", "gold_relevance_level", "gold_relevance_reason", "category", "priority",
        "priority_score", "highlight_required", "classification_reason", "classification_version",
        "classified_at", "updated_at",
    }
    assert row["classification"]["event_id"] == row["event_id"]


def test_repeated_runs_do_not_duplicate(run, settings):
    for _ in range(3):
        run("--classify", "--week")
    with SQLiteRepository(settings.database_path) as db:
        assert db.count() == 23 == db.count_classifications()


def test_step_1_view_is_unchanged_by_classification(run):
    _, before = run("--week")
    run("--classify", "--week")
    _, after = run("--week")
    assert after.out == before.out
    assert before.out.startswith("FOREX FACTORY — USD / GOLD EVENTS")
    assert "Priority" not in after.out and "Category" not in after.out
    # The Step 1 keyword flag still drives the Step 1 --gold filter (13), not the new classification (14).
    _, legacy = run("--week", "--gold", "--json")
    assert len(json.loads(legacy.out)) == 13
    assert "classification" not in json.loads(legacy.out)[0]


def test_new_event_name_is_reported_until_a_rule_exists(run, settings, parse, feed_text):
    extra = replace(parse(feed_text).events[1], event_id="ff-extra", currency="USD", date="2026-10-09",
                    datetime_utc=None, event_name="Brand New Indicator")
    with SQLiteRepository(settings.database_path) as db:
        db.upsert_events([extra])
    _, out = run("--classify", "--all", "--no-fetch")
    assert "No rule yet for (classified by the default rule): Brand New Indicator" in out.out


def test_broken_rule_file_is_a_configuration_error(run, tmp_path):
    bad = tmp_path / "rules.json"
    bad.write_text("{not json", encoding="utf-8")
    code, out = run("--classify", priority_rules_path=bad)
    assert code == 2
    assert out.err.startswith("CONFIGURATION ERROR: The classification rule file")
    # The Step 1 view does not depend on the rule file at all.
    code, out = run("--week", priority_rules_path=bad)
    assert code == 0 and "23 event(s)" in out.out
