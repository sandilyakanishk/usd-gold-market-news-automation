"""Presentation rules added with template version 2: the Gold relevance display scale, the
"why it matters" line, event ordering, and the India-time guarantees. Nothing here sends a message."""

import copy
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from src.classification.rules import PriorityRules
from src.collector.parser import make_event_id
from src.config import PROJECT_ROOT
from src.content.builder import ContentBuilder, build_content_builder, clock_face
from src.content.fixtures import load_preview_fixture, make_item
from src.content.templates import MessageTemplates, TemplateConfigError

TEMPLATES_PATH = PROJECT_ROOT / "config" / "message_templates.json"
RULES = PriorityRules.from_file(PROJECT_ROOT / "config" / "gold_priority_rules.json")
NOW = datetime(2026, 11, 12, 3, 30, tzinfo=timezone.utc)        # 09:00 India time


@pytest.fixture(scope="module")
def template_config():
    return json.loads(TEMPLATES_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def sample(settings):
    items, now = load_preview_fixture(settings)
    return items, build_content_builder(settings, now)


def item(name, release_utc, impact="High", now=NOW, **extra):
    return make_item({"name": name, "release_utc": release_utc, "impact": impact, **extra},
                     rules=RULES, display_timezone="Asia/Kolkata", now=now)


def builder_at(settings, now=NOW, tz="Asia/Kolkata"):
    return build_content_builder(replace(settings, display_timezone=tz), now)


def all_messages(builder, items, day=date(2026, 11, 12)):
    return [builder.morning_update(items, day)] + builder.high_alerts(items) + builder.actual_results(items) \
        + builder.upcoming_reminders(items)


# -- Gold relevance display scale ---------------------------------------------------------

@pytest.mark.parametrize("name, level, score, label", [
    ("CPI m/m", "STRONG", "100", "🟢 STRONG"),
    ("Unemployment Claims", "MODERATE", "65", "🟡 MODERATE"),
    ("Existing Home Sales", "WEAK", "30", "🟠 WEAK"),
    ("Crude Oil Inventories", "NONE", "0", "⚪ NONE"),
])
def test_gold_relevance_display_scale(settings, name, level, score, label):
    values = builder_at(settings).variables(item(name, "2026-11-12T13:30:00Z"))
    assert (values["gold_relevance_level"], values["gold_relevance_score"], values["gold_label"]) == (level, score, label)


def test_display_scale_is_separate_from_the_priority_score(settings, template_config):
    """Changing the display numbers must not move the Step 2 score, the priority or the selection."""
    stored = item("Unemployment Claims", "2026-11-12T13:30:00Z", impact="Medium")
    before = copy.deepcopy(stored.classification)
    normal = builder_at(settings)

    custom = copy.deepcopy(template_config)
    custom["labels"]["gold_relevance_score"].update(STRONG=7, MODERATE=3, WEAK=2, NONE=1)
    changed = ContentBuilder(MessageTemplates(custom), normal.headlines, display_timezone="Asia/Kolkata", now=NOW)

    a, b = normal.variables(stored), changed.variables(stored)
    assert (a["gold_relevance_score"], b["gold_relevance_score"]) == ("65", "3")
    for unchanged in ("priority", "priority_score", "gold_relevance_level", "highlight_required", "impact", "event_name"):
        assert a[unchanged] == b[unchanged]
    assert (a["priority"], a["priority_score"]) == ("MEDIUM", "60")      # 20 + 25 + 15, from Step 2 alone
    assert stored.classification == before                                # the stored classification is untouched
    assert [e["event_name"] for e in normal.morning_update([stored], date(2026, 11, 12)).events] == \
        [e["event_name"] for e in changed.morning_update([stored], date(2026, 11, 12)).events]


def test_classification_is_exactly_what_step_2_produced(sample):
    items, builder = sample
    for entry in items:
        fresh = RULES.classify(entry.event.event_id, entry.event.currency, entry.event.event_name, entry.event.impact)
        snapshot = copy.deepcopy(entry.classification)
        values = builder.variables(entry)
        assert entry.classification == snapshot == fresh
        assert (values["priority"], values["priority_score"], values["gold_relevance_level"]) == (
            fresh.priority, str(fresh.priority_score), fresh.gold_relevance_level)
        assert values["event_name"] == entry.event.event_name


def test_the_three_concepts_are_shown_separately(sample):
    items, builder = sample
    (alert,) = [m for m in builder.high_alerts(items) if m.event_name == "FOMC Meeting Minutes"]
    assert "📊 Impact: 🔴 High\n🥇 Gold Relevance: 🟢 STRONG · 100/100\n🎯 Priority: 🟠 HIGH · 85/100" in alert.text
    # Impact is Forex Factory's own word, exactly as stored; the other two are the bot's.
    claims = builder.variables(next(i for i in items if i.event.event_name == "Unemployment Claims"))
    assert (claims["impact"], claims["impact_label"]) == ("Medium", "🟡 Medium")
    assert (claims["gold_label"], claims["gold_relevance_score"], claims["priority_label"]) == ("🟡 MODERATE", "65", "🟡 MEDIUM")


def test_the_scale_is_never_presented_as_a_probability_or_a_forecast(sample, template_config):
    items, builder = sample
    text = "\n".join(m.text for m in all_messages(builder, items)).casefold()
    for word in ("chance", "probab", "likel", "odds", "expected move", "will rise", "will fall", "will move",
                 "bullish", "bearish", "buy", "sell", "%/100", "% chance"):
        assert word not in text, word
    assert "not a prediction of price direction" in text
    note = template_config["labels"]["gold_relevance_score"]["_about"]
    assert "Not a probability" in note and "monitoring framework" in note


@pytest.mark.parametrize("mutate, message", [
    (lambda c: c["labels"]["gold_relevance_score"].pop("WEAK"), "gold_relevance_score.WEAK"),
    (lambda c: c["labels"]["gold_relevance_score"].update(STRONG=101), "gold_relevance_score.STRONG"),
    (lambda c: c["labels"]["gold_relevance_score"].update(NONE="zero"), "gold_relevance_score.NONE"),
    (lambda c: c["labels"]["alert_title"].pop("default"), "alert_title needs a 'default'"),
    (lambda c: c["selection"].update(daily_order="random"), "daily_order must be"),
    (lambda c: c.update(omit_line_if_missing={"default": [], "WEEKLY_DIGEST": ["actual"]}), "unknown message type"),
])
def test_broken_presentation_settings_are_rejected(template_config, mutate, message):
    custom = copy.deepcopy(template_config)
    mutate(custom)
    with pytest.raises(TemplateConfigError, match=message):
        MessageTemplates(custom)


# -- why it matters ---------------------------------------------------------------------------

@pytest.mark.parametrize("name, category, text", [
    ("Federal Funds Rate", "MONETARY_POLICY", "Fed policy decisions can shift interest-rate expectations and the USD, both of which Gold is sensitive to."),
    ("CPI m/m", "INFLATION", "Inflation data can shift rate expectations and USD and yield pricing, making it relevant to Gold."),
    ("Non-Farm Employment Change", "EMPLOYMENT", "Labour-market data can influence Fed expectations, the USD and Treasury yields."),
    ("Advance GDP q/q", "GROWTH", "Growth data can affect rate expectations and broader risk sentiment."),
    ("Retail Sales m/m", "CONSUMER_ACTIVITY", "Consumer data can influence growth expectations and the policy outlook."),
    ("FOMC Member Waller Speaks", "FED_COMMUNICATION", "Fed communication can influence rate expectations, the USD and Gold."),
])
def test_why_it_matters_follows_the_step_2_category(settings, name, category, text):
    entry = item(name, "2026-11-12T13:30:00Z")
    values = builder_at(settings).variables(entry)
    assert (entry.classification.category, values["category"], values["why_it_matters"]) == (category, category, text)


def test_why_it_matters_never_states_a_direction(template_config):
    explanations = {k: v for k, v in template_config["why_it_matters"].items() if not k.startswith("_")}
    assert set(explanations) >= {"MONETARY_POLICY", "FED_COMMUNICATION", "INFLATION", "EMPLOYMENT", "GROWTH", "CONSUMER_ACTIVITY"}
    for category, text in explanations.items():
        lowered = text.casefold()
        for word in ("will ", "rise", "fall", "higher", "lower", "bullish", "bearish", "buy", "sell", "up ", "down "):
            assert word not in lowered, (category, word)


def test_uncategorised_event_falls_back_to_the_step_2_reason(settings):
    entry = item("A Brand New Indicator", "2026-11-12T13:30:00Z")
    values = builder_at(settings).variables(entry)
    assert entry.classification.category == "OTHER"
    assert values["why_it_matters"] == entry.classification.gold_relevance_reason


# -- morning brief: numbering and order ---------------------------------------------------------

def test_morning_events_are_ordered_by_priority_then_relevance_then_time(settings, template_config):
    items = [
        item("Unemployment Claims", "2026-11-12T06:30:00Z", impact="Medium"),        # 12:00  MEDIUM / MODERATE
        item("FOMC Meeting Minutes", "2026-11-12T08:30:00Z"),                         # 14:00  HIGH / STRONG
        item("JOLTS Job Openings", "2026-11-12T07:30:00Z"),                           # 13:00  HIGH / MODERATE
        item("CPI m/m", "2026-11-12T13:30:00Z"),                                      # 19:00  CRITICAL / STRONG
        item("Core CPI m/m", "2026-11-12T11:30:00Z"),                                 # 17:00  CRITICAL / STRONG
        item("Retail Sales m/m", "2026-11-12T09:30:00Z"),                             # 15:00  HIGH / STRONG
    ]
    builder = builder_at(settings)
    message = builder.morning_update(items, date(2026, 11, 12))
    assert [e["event_name"] for e in message.events] == [
        "Core CPI m/m", "CPI m/m",                 # CRITICAL, by time
        "FOMC Meeting Minutes", "Retail Sales m/m",  # HIGH + STRONG, by time
        "JOLTS Job Openings",                      # HIGH + MODERATE
        "Unemployment Claims",                     # MEDIUM
    ]
    # Numbered in that order, each with its own time still visible.
    for number, (name, shown) in zip(["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣"], [
            ("Core CPI m/m", "🕔 5:00 PM IST"), ("CPI m/m", "🕖 7:00 PM IST"), ("FOMC Meeting Minutes", "🕑 2:00 PM IST"),
            ("Retail Sales m/m", "🕒 3:00 PM IST"), ("JOLTS Job Openings", "🕐 1:00 PM IST"), ("Unemployment Claims", "🕛 12:00 PM IST")]):
        assert f"{number} *{name}*\n" in message.text
        block = message.text.split(f"{number} *{name}*\n", 1)[1].split("\n\n", 1)[0]
        assert shown in block

    # The input list is not reordered, and plain chronological order is one setting away.
    assert items[0].event.event_name == "Unemployment Claims"
    custom = copy.deepcopy(template_config)
    custom["selection"]["daily_order"] = "time"
    by_time = ContentBuilder(MessageTemplates(custom), builder.headlines, display_timezone="Asia/Kolkata", now=NOW)
    assert [e["event_name"] for e in by_time.morning_update(items, date(2026, 11, 12)).events] == [
        "Unemployment Claims", "JOLTS Job Openings", "FOMC Meeting Minutes", "Retail Sales m/m", "Core CPI m/m", "CPI m/m"]


def test_numbering_continues_past_the_emoji_list(settings):
    items = [item(f"FOMC Member Speaker{n:02d} Speaks", f"2026-11-12T{4 + n:02d}:30:00Z", impact="Medium") for n in range(12)]
    text = builder_at(settings).morning_update(items, date(2026, 11, 12)).text
    assert "🔟 *FOMC Member Speaker09 Speaks*" in text and "11. *FOMC Member Speaker10 Speaks*" in text
    assert "12. *FOMC Member Speaker11 Speaks*" in text


def test_morning_brief_omits_empty_figures_but_other_messages_show_a_dash(sample):
    items, builder = sample
    speech = item("FOMC Member Waller Speaks", "2026-11-12T08:30:00Z", impact="Medium")
    brief = builder.morning_update([speech], date(2026, 11, 12)).text
    assert "Previous" not in brief and "Forecast" not in brief and "Actual" not in brief and "—" not in brief
    assert "💡 Fed communication can influence rate expectations, the USD and Gold." in brief
    (reminder,) = [m for m in builder.upcoming_reminders(items) if m.event_name == "FOMC Meeting Minutes"]
    assert "Previous: —\nForecast: —" in reminder.text
    for message in all_messages(builder, items):
        for raw in ("None", "null", "N/A", "undefined", "nan"):
            assert raw not in message.text


def test_alert_title_reflects_forex_factory_impact(settings):
    builder = builder_at(settings)
    high = item("CPI m/m", "2026-11-12T13:30:00Z", impact="High")
    medium = item("Fed Chair Powell Speaks", "2026-11-12T13:30:00Z", impact="Medium")   # CRITICAL priority, Medium impact
    assert medium.classification.priority == "CRITICAL" and medium.classification.highlight_required
    titles = {m.event_name: m.text.splitlines()[1] for m in builder.high_alerts([high, medium])}
    assert titles == {"CPI m/m": "🚨 *HIGH-IMPACT USD ALERT*", "Fed Chair Powell Speaks": "🚨 *PRIORITY USD ALERT*"}


# -- India time -----------------------------------------------------------------------------------

@pytest.mark.parametrize("release_utc, shown_time, shown_date, weekday, clock", [
    ("2026-10-08T08:30:00Z", "2:00 PM IST", "8 October 2026", "Thursday", "🕑"),      # 08:30 UTC -> 14:00 IST
    ("2026-10-07T18:00:00Z", "11:30 PM IST", "7 October 2026", "Wednesday", "🕦"),    # 18:00 UTC -> 23:30 IST
    ("2026-10-08T12:30:00Z", "6:00 PM IST", "8 October 2026", "Thursday", "🕕"),
    ("2026-10-08T18:30:00Z", "12:00 AM IST", "9 October 2026", "Friday", "🕛"),       # rolls over to the next day
    ("2026-10-09T20:00:00Z", "1:30 AM IST", "10 October 2026", "Saturday", "🕜"),     # Friday in New York, Saturday in India
    ("2026-12-31T19:00:00Z", "12:30 AM IST", "1 January 2027", "Friday", "🕧"),       # rolls over the year
    ("2026-10-08T00:00:00Z", "5:30 AM IST", "8 October 2026", "Thursday", "🕠"),
    ("2026-10-08T06:30:00Z", "12:00 PM IST", "8 October 2026", "Thursday", "🕛"),
])
def test_utc_to_india_time(settings, release_utc, shown_time, shown_date, weekday, clock):
    entry = item("CPI m/m", release_utc, now=datetime(2026, 10, 1, tzinfo=timezone.utc))
    values = builder_at(settings).variables(entry)
    assert (values["display_time"], values["date"], values["weekday"], values["clock"]) == (shown_time, shown_date, weekday, clock)
    assert entry.event.datetime_utc == release_utc          # the stored instant is never altered


def test_us_daylight_saving_does_not_corrupt_india_time(settings, parse):
    """The same 08:30 New York release is 18:00 IST in US summer time and 19:00 IST in US winter time."""
    feed = json.dumps([
        {"title": "CPI m/m", "country": "USD", "date": "2026-10-14T08:30:00-04:00", "impact": "High"},   # EDT
        {"title": "CPI m/m", "country": "USD", "date": "2026-11-12T08:30:00-05:00", "impact": "High"},   # EST
        {"title": "CPI m/m", "country": "USD", "date": "2026-03-11T08:30:00-04:00", "impact": "High"},   # EDT again
    ])
    summer, winter, spring = parse(feed).events
    assert [e.datetime_utc for e in (summer, winter, spring)] == [
        "2026-10-14T12:30:00Z", "2026-11-12T13:30:00Z", "2026-03-11T12:30:00Z"]
    assert [(e.date, e.time, e.timezone) for e in (summer, winter, spring)] == [
        ("2026-10-14", "18:00", "Asia/Kolkata"), ("2026-11-12", "19:00", "Asia/Kolkata"), ("2026-03-11", "18:00", "Asia/Kolkata")]
    builder = builder_at(settings)
    shown = []
    for event in (summer, winter, spring):
        entry = item("CPI m/m", event.datetime_utc)
        assert entry.event.event_id == make_event_id(event.datetime_utc, "USD", "CPI m/m")
        shown.append(builder.variables(replace(entry, event=event))["display_time"])
    assert shown == ["6:00 PM IST", "7:00 PM IST", "6:00 PM IST"]


def test_display_is_converted_once_from_the_utc_instant(settings):
    """The stored local date/time fields are ignored for display, so a stale or wrong one cannot double-convert."""
    before_release = datetime(2026, 10, 1, tzinfo=timezone.utc)
    entry = item("CPI m/m", "2026-10-08T08:30:00Z", now=before_release)
    builder = builder_at(settings, now=before_release)
    wrong_local = replace(entry, event=replace(entry.event, date="2026-10-08", time="08:30", timezone="UTC"))
    assert builder.variables(wrong_local)["display_time"] == builder.variables(entry)["display_time"] == "2:00 PM IST"
    # Converting an already-converted value would give 7:30 PM; that must never appear.
    assert "7:30 PM" not in builder.upcoming_reminders([entry])[0].text


def test_display_zone_comes_from_settings_never_from_the_machine(settings):
    entry = item("CPI m/m", "2026-10-08T08:30:00Z")
    assert builder_at(settings).variables(entry)["display_time"] == "2:00 PM IST"
    assert builder_at(settings, tz="America/New_York").variables(entry)["display_time"] == "4:30 AM EDT"
    assert builder_at(settings, tz=None).variables(entry)["display_time"] == "8:30 AM UTC"
    source = "".join((PROJECT_ROOT / "src" / "content" / name).read_text(encoding="utf-8")
                     for name in ("builder.py", "templates.py", "headlines.py", "fixtures.py"))
    for naive in ("datetime.now()", "datetime.today()", "date.today()", "time.localtime", ".astimezone()", "utcnow("):
        assert naive not in source, naive


@pytest.mark.parametrize("machine_zone", ["UTC", "America/Los_Angeles", "Pacific/Kiritimati", "Asia/Kolkata"])
def test_output_is_identical_whatever_the_machine_timezone(machine_zone):
    """A fresh process with a different TZ (as on a cloud runner) must print the same India times."""
    env = {**os.environ, "TZ": machine_zone, "PYTHONIOENCODING": "utf-8", "DATABASE_BACKEND": "sqlite",
           "DISPLAY_TIMEZONE": "Asia/Kolkata"}
    done = subprocess.run([sys.executable, "-m", "src.main", "--preview-morning", "--preview-upcoming", "--fixture"],
                          cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert done.returncode == 0, done.stderr
    assert "📅 Thursday, 12 November 2026" in done.stdout
    assert "🕖 7:00 PM IST" in done.stdout                                  # 13:30 UTC
    assert "🕧 12:30 AM IST · Saturday, 14 November 2026" in done.stdout    # 19:00 UTC on the 13th
    for leak in (" UTC", "PST", "PDT", "GMT", "+05:30", "+00:00"):
        assert leak not in done.stdout, leak


def test_morning_brief_uses_the_india_calendar_date(settings):
    """At 20:00 UTC on the 7th it is already 01:30 on the 8th in India: 'today' follows India."""
    late_utc = datetime(2026, 10, 7, 20, 0, tzinfo=timezone.utc)
    builder = builder_at(settings, now=late_utc)
    india_today = late_utc.astimezone(builder.tz).date()
    assert india_today == date(2026, 10, 8) and late_utc.date() == date(2026, 10, 7)
    events = [item("Unemployment Claims", "2026-10-08T12:30:00Z", impact="Medium", now=late_utc),     # 8 Oct, 18:00 IST
              item("FOMC Meeting Minutes", "2026-10-07T18:00:00Z", now=late_utc),                      # 7 Oct, 23:30 IST
              item("FOMC Member Collins Speaks", "2026-10-08T20:00:00Z", impact="Medium", now=late_utc)]  # 9 Oct, 01:30 IST
    message = builder.morning_update(events, india_today)
    assert message.message_key == "DAILY_UPDATE_2026-10-08"
    assert "📅 Thursday, 8 October 2026" in message.text
    assert [e["event_name"] for e in message.events] == ["Unemployment Claims"]       # only events on India's 8th
    assert "due today" in message.text


def test_cli_today_is_the_india_date_not_the_utc_date(monkeypatch, settings, capsys, parse, feed_text):
    from src import main as cli
    from src.database.database import SQLiteRepository
    from src.classification.service import classify_events

    with SQLiteRepository(settings.database_path) as db:
        db.upsert_events(e for e in parse(feed_text).events if e.currency == "USD")
        classify_events(settings, db)
    monkeypatch.setattr(cli.Settings, "from_env", classmethod(lambda cls: settings))

    class Clock(cli.datetime):
        @classmethod
        def now(cls, tz=None):                       # 20:00 UTC on 7 Oct == 01:30 on 8 Oct in India
            return cls(2026, 10, 7, 20, 0, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(cli, "datetime", Clock)
    assert cli.main(["--preview-morning"]) == 0
    out = capsys.readouterr().out
    assert "===== DAILY_UPDATE_2026-10-08 | MORNING_UPDATE =====" in out and "📅 Thursday, 8 October 2026" in out


@pytest.mark.parametrize("hour, minute, face", [
    (14, 0, "🕑"), (14, 14, "🕑"), (14, 15, "🕝"), (14, 30, "🕝"), (14, 44, "🕝"), (14, 45, "🕒"),
    (0, 0, "🕛"), (12, 0, "🕛"), (0, 30, "🕧"), (23, 50, "🕛"), (1, 30, "🕜"), (18, 0, "🕕"), (19, 1, "🕖"),
])
def test_clock_face(hour, minute, face):
    assert clock_face(datetime(2026, 10, 8, hour, minute)) == face


def test_leading_zero_setting(settings, template_config):
    entry = item("CPI m/m", "2026-10-08T08:30:00Z")
    assert builder_at(settings).variables(entry)["display_time"] == "2:00 PM IST"
    custom = copy.deepcopy(template_config)
    custom["time"]["strip_leading_zeros"] = False
    padded = ContentBuilder(MessageTemplates(custom), builder_at(settings).headlines, display_timezone="Asia/Kolkata", now=NOW)
    values = padded.variables(entry)
    assert (values["display_time"], values["date"]) == ("02:00 PM IST", "08 October 2026")
    midnight = item("CPI m/m", "2026-10-08T18:30:00Z")
    assert builder_at(settings).variables(midnight)["display_time"] == "12:00 AM IST"   # only leading zeros go
