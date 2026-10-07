"""Preview commands: text generation only. They read; they never fetch, classify, enrich, write or send."""

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
    return {"BLS": FixtureProvider("BLS"),
            "FRED": FixtureProvider("FRED", {"ICSA": {date(2026, 10, 3): Decimal("218000")}})}


@pytest.fixture
def run(monkeypatch, settings, feed_text, capsys, sources):
    """CLI on the fixture feed week. 'Now' is 8 Oct 2026 09:00 India time unless a test moves it."""
    state = {"settings": settings, "now": (2026, 10, 8, 9, 0), "network": []}
    monkeypatch.setattr(cli.Settings, "from_env", classmethod(lambda cls: state["settings"]))
    monkeypatch.setattr(forex_factory.urllib.request, "urlopen", fake_urlopen(feed_text, calls=state["network"]))
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
    _run.network = state["network"]
    return _run


@pytest.fixture
def prepared(run):
    """Database filled the normal way: calendar synced, classified, actuals checked (claims not released yet)."""
    run("--enrich-actuals", "--week")
    run.network.clear()
    return run


def test_morning_preview_from_the_database(prepared):
    code, out = prepared("--preview-morning")
    assert code == 0
    assert out.out == """===== DAILY_UPDATE_2026-10-08 | MORNING_UPDATE =====
━━━━━━━━━━━━━━━━━━
🇺🇸 *USD + GOLD MORNING BRIEF*
📅 Thursday, 8 October 2026
━━━━━━━━━━━━━━━━━━

📌 *KEY EVENTS*

1️⃣ *FOMC Member Waller Speaks*
📰 Fed official Waller due to speak today
🕑 2:00 PM IST
📊 Impact: 🟡 Medium
🥇 Gold Relevance: 🟡 MODERATE · 65/100
🎯 Priority: 🟡 MEDIUM
💡 Fed communication can influence rate expectations, the USD and Gold.

2️⃣ *Unemployment Claims*
📰 US weekly jobless claims due today
🕕 6:00 PM IST
📊 Impact: 🟡 Medium
🥇 Gold Relevance: 🟡 MODERATE · 65/100
🎯 Priority: 🟡 MEDIUM
Previous: 197K
Forecast: 200K
💡 Labour-market data can influence Fed expectations, the USD and Treasury yields.

━━━━━━━━━━━━━━━━━━
👀 *MARKET FOCUS*

• Watch the USD reaction around each release.
• Watch Gold volatility if a figure differs materially from expectations.
• Higher-priority events deserve the most attention.

⚠️ Gold relevance is this bot's monitoring assessment, not a prediction of price direction.
━━━━━━━━━━━━━━━━━━
Source: Forex Factory
"""


def test_morning_preview_for_another_day(prepared):
    _, out = prepared("--preview-morning", "--tomorrow")
    assert "===== DAILY_UPDATE_2026-10-09 | MORNING_UPDATE =====" in out.out
    assert "📰 US consumer sentiment survey due tomorrow" in out.out and "*Prelim UoM Consumer Sentiment*" in out.out
    _, out = prepared("--preview-morning", "--date", "2026-10-07")
    assert "DAILY_UPDATE_2026-10-07" in out.out
    assert "📰 Fed meeting minutes published" in out.out and "*FOMC Meeting Minutes*" in out.out
    _, out = prepared("--preview-morning", "--date", "2026-10-04")
    assert "No major USD / Gold events are scheduled for this day." in out.out


def test_previews_with_no_matching_event_say_so(prepared):
    code, out = prepared("--preview-alert", "--preview-actuals", "--preview-upcoming")
    assert code == 0
    for kind in ("HIGH_ALERT", "ACTUAL_RESULT", "UPCOMING_REMINDER"):
        assert f"No {kind} message for the selected events. Add --fixture to preview" in out.out
    _, out = prepared("--preview-alert", "--json")
    assert json.loads(out.out) == []


def test_actual_preview_appears_once_the_figure_is_released(prepared):
    prepared("--enrich-actuals", "--week", now=(2026, 10, 8, 18, 30))   # claims released at 18:00 IST
    _, out = prepared("--preview-actuals", "--json")
    (message,) = json.loads(out.out)
    assert message["message_type"] == "ACTUAL_RESULT" and message["message_key"] == f"ACTUAL_{message['event_id']}_1"
    assert (message["event_name"], message["headline"]) == ("Unemployment Claims", "🇺🇸 US weekly jobless claims come in above expectations")
    assert "Actual: *218K*" in message["text"] and "*Result:* 📈 ABOVE FORECAST\n+18K vs forecast" in message["text"]
    assert "Source: FRED (calendar: Forex Factory)" in message["text"]
    assert message["attribution"].startswith("This product uses the FRED® API")
    # The same event in the daily update now carries the figure and the new headline.
    _, out = prepared("--preview-morning")
    assert "📰 US weekly jobless claims come in above expectations" in out.out and "Actual: 218K" in out.out


def test_upcoming_and_alert_follow_configuration(prepared, tmp_path, settings):
    config = json.loads(settings.message_templates_path.read_text(encoding="utf-8"))
    config["selection"].update(upcoming_minimum_priority="MEDIUM", alert_only_before_release=False)
    custom = tmp_path / "templates.json"
    custom.write_text(json.dumps(config), encoding="utf-8")
    _, out = prepared("--preview-upcoming", "--json", message_templates_path=custom)
    assert [m["event_name"] for m in json.loads(out.out)] == [
        "FOMC Member Waller Speaks", "Unemployment Claims", "Prelim UoM Consumer Sentiment", "Prelim UoM Inflation Expectations"]
    _, out = prepared("--preview-alert", "--json", message_templates_path=custom)
    (alert,) = json.loads(out.out)
    assert (alert["event_name"], alert["priority"], alert["highlight_required"]) == ("FOMC Meeting Minutes", "HIGH", True)


@pytest.mark.parametrize("flag, count, first_key_prefix", [
    ("--preview-morning", 1, "DAILY_UPDATE_2026-11-12"),
    ("--preview-alert", 4, "HIGH_ALERT_ff-"),
    ("--preview-actuals", 10, "ACTUAL_ff-"),
    ("--preview-upcoming", 5, "UPCOMING_ff-"),
])
def test_fixture_previews(run, settings, flag, count, first_key_prefix):
    code, out = run(flag, "--fixture", "--json")
    assert code == 0
    messages = json.loads(out.out)
    assert len(messages) == count and messages[0]["message_key"].startswith(first_key_prefix)
    assert set(messages[0]) == {
        "message_key", "message_type", "event_id", "event_name", "headline", "priority", "highlight_required",
        "text", "generated_at", "events", "attribution", "markdown_safe"}
    assert all(m["generated_at"] == "2026-11-12T03:30:00Z" for m in messages)
    assert not settings.database_path.exists()  # sample mode never opens a database


def test_fixture_text_output_is_labelled_as_sample_data(run):
    _, out = run("--preview-alert", "--fixture")
    assert out.out.startswith("SAMPLE DATA: these messages are built from bundled sample events, not from the database.")
    assert "===== HIGH_ALERT_ff-" in out.out and "🚨 *HIGH-IMPACT USD ALERT*" in out.out
    assert "🇺🇸 *CPI m/m*\n📰 US CPI inflation data due today" in out.out


def test_previews_are_read_only(prepared, settings, sources):
    with SQLiteRepository(settings.database_path) as db:
        before = (db.query_events(), db.get_classifications([e.event_id for e in db.query_events()]),
                  db.get_actual_records([e.event_id for e in db.query_events()]))
    mtime = settings.database_path.stat().st_mtime_ns
    requests_before = len(sources["FRED"].requests) + len(sources["BLS"].requests)
    for argv in (["--preview-morning"], ["--preview-alert"], ["--preview-actuals"], ["--preview-upcoming"],
                 ["--preview-morning", "--preview-alert", "--preview-actuals", "--preview-upcoming", "--json"]):
        assert prepared(*argv)[0] == 0
    with SQLiteRepository(settings.database_path) as db:
        after = (db.query_events(), db.get_classifications([e.event_id for e in db.query_events()]),
                 db.get_actual_records([e.event_id for e in db.query_events()]))
    assert after == before
    assert settings.database_path.stat().st_mtime_ns == mtime
    assert prepared.network == []                                                     # no calendar download
    assert len(sources["FRED"].requests) + len(sources["BLS"].requests) == requests_before  # no source lookup


def test_preview_output_is_repeatable(prepared):
    first = prepared("--preview-morning")[1].out
    assert prepared("--preview-morning")[1].out == first
    a = json.loads(prepared("--preview-morning", "--json")[1].out)
    b = json.loads(prepared("--preview-morning", "--json", now=(2026, 10, 8, 9, 5))[1].out)
    assert (a[0]["text"], a[0]["message_key"]) == (b[0]["text"], b[0]["message_key"])


def test_fixture_needs_a_preview_option(run):
    code, out = run("--fixture")
    assert code == 2 and "only applies together with a --preview-" in out.err
    code, out = run("--week", "--fixture")
    assert code == 2


def test_invalid_preview_date(prepared):
    code, out = prepared("--preview-morning", "--date", "08-10-2026")
    assert code == 2 and "Invalid date" in out.err


@pytest.mark.parametrize("setting, text", [
    ("message_templates_path", "The message template file"),
    ("headline_rules_path", "The headline rule file"),
])
def test_broken_content_configuration_is_reported(prepared, tmp_path, setting, text):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    code, out = prepared("--preview-morning", **{setting: bad})
    assert code == 2 and out.err.startswith(f"CONFIGURATION ERROR: {text}")
    # Steps 1 to 3 do not depend on the content configuration.
    assert prepared("--week", "--no-fetch", **{setting: bad})[0] == 0
    assert prepared("--classify", "--no-fetch", **{setting: bad})[0] == 0
    assert prepared("--actuals", "--no-fetch", **{setting: bad})[0] == 0


def test_earlier_views_are_unchanged_by_previews(prepared):
    views = (["--week", "--no-fetch"], ["--classify", "--week", "--no-fetch"], ["--actuals", "--week", "--no-fetch"])
    before = [prepared(*v)[1].out for v in views]
    prepared("--preview-morning", "--preview-alert", "--preview-actuals", "--preview-upcoming")
    prepared("--preview-morning", "--fixture")
    assert [prepared(*v)[1].out for v in views] == before
    assert before[0].startswith("FOREX FACTORY — USD / GOLD EVENTS") and "📰" not in "".join(before)


def test_the_content_layer_has_no_way_to_send_anything():
    """It imports only text, date and project modules: no network, no process, no messaging client."""
    import ast
    import pathlib
    import src.content as content
    allowed = {"__future__", "json", "re", "string", "dataclasses", "datetime", "pathlib", "zoneinfo"}
    imported = set()
    for path in pathlib.Path(content.__path__[0]).glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                imported.add(node.module.split(".")[0])
    assert imported <= allowed, imported - allowed
