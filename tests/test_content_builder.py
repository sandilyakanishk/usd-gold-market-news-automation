"""Templates and the content builder, driven by the bundled sample events."""

import copy
import json
from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from src.actuals.models import FAILED, NO_DATA, UPCOMING, ActualRecord
from src.config import PROJECT_ROOT
from src.content.builder import ContentBuilder, build_content_builder
from src.content.fixtures import load_preview_fixture
from src.content.headlines import HeadlineRules
from src.content.templates import EVENT_VARIABLES, MessageTemplates, TemplateConfigError, markdown_safe

TEMPLATES_PATH = PROJECT_ROOT / "config" / "message_templates.json"
DAY = date(2026, 11, 12)


@pytest.fixture(scope="module")
def template_config():
    return json.loads(TEMPLATES_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def sample(settings):
    """(items, now, builder) for the sample events; 'now' is 09:00 India time on 12 Nov 2026."""
    items, now = load_preview_fixture(settings)
    return items, now, build_content_builder(settings, now)


def pick(items, name, released=None):
    """A sample event by name; `released` chooses between the upcoming and the released copy."""
    found = [i for i in items if i.event.event_name == name
             and (released is None or (i.record.release_status == "RELEASED") == released)]
    assert len(found) == 1, (name, len(found))
    return found[0]


def custom_builder(template_config, sample, mutate):
    config = copy.deepcopy(template_config)
    mutate(config)
    _, now, builder = sample
    return ContentBuilder(MessageTemplates(config), builder.headlines, display_timezone="Asia/Kolkata", now=now)


# -- morning update ---------------------------------------------------------------

def test_morning_update_text(sample):
    items, _, builder = sample
    message = builder.morning_update(items, DAY)
    assert message.text == """📅 *USD + GOLD DAILY UPDATE*
━━━━━━━━━━━━━━━━━━
📆 Thursday, 12 Nov 2026

📰 US CPI inflation data due today
🇺🇸 07:00 PM IST
*CPI m/m*
Impact: 🔴 HIGH
Gold: 🟢 STRONG
Priority: 🔴 CRITICAL
Previous: 0.4%
Forecast: 0.3%

┄┄┄┄┄┄┄┄┄

📰 US core CPI inflation data due today
🇺🇸 07:00 PM IST
*Core CPI m/m*
Impact: 🔴 HIGH
Gold: 🟢 STRONG
Priority: 🔴 CRITICAL
Previous: 0.3%
Forecast: 0.3%

┄┄┄┄┄┄┄┄┄

📰 US weekly jobless claims due today
🇺🇸 07:00 PM IST
*Unemployment Claims*
Impact: 🟠 MEDIUM
Gold: 🟡 MODERATE
Priority: 🟡 MEDIUM
Previous: 218K
Forecast: 220K

━━━━━━━━━━━━━━━━━━
Source: Forex Factory"""
    assert (message.message_key, message.message_type) == ("DAILY_UPDATE_2026-11-12", "MORNING_UPDATE")
    assert (message.event_id, message.event_name, message.headline) == (None, None, None)
    assert (message.priority, message.highlight_required, message.markdown_safe) == ("CRITICAL", True, True)
    assert [e["event_name"] for e in message.events] == ["CPI m/m", "Core CPI m/m", "Unemployment Claims"]
    assert all(e["headline"] and e["headline"] != e["event_name"] and e["event_id"] for e in message.events)


def test_morning_update_leaves_out_low_priority_and_non_gold_events(sample):
    items, _, builder = sample
    text = builder.morning_update(items, DAY).text
    for excluded in ("FOMC Member Williams Speaks", "Natural Gas Storage", "30-y Bond Auction"):
        assert excluded not in text
    # ...and events of other days.
    for other_day in ("Federal Funds Rate", "Non-Farm Employment Change", "Retail Sales m/m"):
        assert other_day not in text


def test_daily_minimum_priority_is_configurable(template_config, sample):
    items = sample[0]
    names = lambda b: [e["event_name"] for e in b.morning_update(items, DAY).events]
    low = custom_builder(template_config, sample, lambda c: c["selection"].update(daily_minimum_priority="LOW"))
    assert names(low) == ["CPI m/m", "Core CPI m/m", "Unemployment Claims", "FOMC Member Williams Speaks"]
    critical = custom_builder(template_config, sample, lambda c: c["selection"].update(daily_minimum_priority="CRITICAL"))
    assert names(critical) == ["CPI m/m", "Core CPI m/m"]
    everything = custom_builder(template_config, sample, lambda c: c["selection"].update(
        daily_minimum_priority="LOW", daily_require_gold_relevance=False))
    assert names(everything)[-2:] == ["Natural Gas Storage", "30-y Bond Auction"] and len(names(everything)) == 6


def test_selection_uses_the_classification_not_the_step_1_keyword_flag(sample):
    items, _, builder = sample
    # Flip the old keyword flag on every event: the result must not move.
    flipped = [replace(i, event=replace(i.event, gold_relevance=not i.event.gold_relevance)) for i in items]
    assert builder.morning_update(flipped, DAY).text == builder.morning_update(items, DAY).text
    assert [m.message_key for m in builder.high_alerts(flipped)] == [m.message_key for m in builder.high_alerts(items)]


def test_morning_update_on_a_day_without_events(sample):
    items, _, builder = sample
    message = builder.morning_update(items, date(2026, 11, 15))
    assert message.text == """📅 *USD + GOLD DAILY UPDATE*
━━━━━━━━━━━━━━━━━━
📆 Sunday, 15 Nov 2026

No major USD / Gold events are scheduled for this day.

━━━━━━━━━━━━━━━━━━
Source: Forex Factory"""
    assert (message.events, message.priority, message.highlight_required) == ([], None, False)
    assert message.message_key == "DAILY_UPDATE_2026-11-15"


def test_morning_update_shows_the_actual_once_it_exists(sample):
    items, _, builder = sample
    text = builder.morning_update(items, date(2026, 11, 6)).text  # payrolls day, already released
    assert "*Non-Farm Employment Change*" in text and "Actual: 85K" in text
    assert "📰 US non-farm payrolls come in above expectations" in text
    assert "Actual:" not in builder.morning_update(items, DAY).text


def test_unclassified_events_are_not_selected(sample):
    items, _, builder = sample
    bare = [replace(i, classification=None) for i in items]
    assert builder.morning_update(bare, DAY).events == []
    assert builder.high_alerts(bare) == [] and builder.upcoming_reminders(bare) == []


# -- high-impact alert --------------------------------------------------------------

def test_high_alert_text(sample):
    items, _, builder = sample
    message = next(m for m in builder.high_alerts(items) if m.event_name == "CPI m/m")
    assert message.text == """🚨 *HIGH IMPACT ALERT*
━━━━━━━━━━━━━━━━━━

📰 US CPI inflation data due today

🇺🇸 *CPI m/m*

⏰ 07:00 PM IST, Thursday 12 Nov 2026

Impact: 🔴 HIGH
Gold: 🟢 STRONG
Priority: 🔴 CRITICAL

Previous: 0.4%
Forecast: 0.3%

━━━━━━━━━━━━━━━━━━
Source: Forex Factory"""
    upcoming_cpi = pick(items, "CPI m/m", released=False)
    assert message.message_key == f"HIGH_ALERT_{upcoming_cpi.event.event_id}"
    assert (message.event_id, message.event_name) == (upcoming_cpi.event.event_id, "CPI m/m")
    assert message.headline == "🇺🇸 US CPI inflation data due today"
    assert (message.priority, message.highlight_required, message.message_type) == ("CRITICAL", True, "HIGH_ALERT")


def test_alerts_only_for_highlighted_upcoming_events(sample):
    items, _, builder = sample
    alerts = builder.high_alerts(items)
    assert [(m.event_name, m.priority) for m in alerts] == [
        ("CPI m/m", "CRITICAL"), ("Core CPI m/m", "CRITICAL"), ("Federal Funds Rate", "CRITICAL"),
        ("FOMC Meeting Minutes", "HIGH")]
    assert all(m.highlight_required for m in alerts)
    # Retail Sales is High impact and HIGH priority but not marked for highlight: no alert.
    assert pick(items, "Retail Sales m/m").classification.priority == "HIGH"
    assert "Retail Sales m/m" not in [m.event_name for m in alerts]
    # A highlighted event whose release has passed gets no alert either (payrolls, 6 Nov).
    assert pick(items, "Non-Farm Employment Change").classification.highlight_required
    assert "Non-Farm Employment Change" not in [m.event_name for m in alerts]


def test_high_minutes_alert_shows_missing_values_cleanly(sample):
    items, _, builder = sample
    message = next(m for m in builder.high_alerts(items) if m.event_name == "FOMC Meeting Minutes")
    assert "📰 Fed meeting minutes due on 19 Nov" in message.text
    assert "🇺🇸 *FOMC Meeting Minutes*" in message.text
    assert "Previous: -\nForecast: -" in message.text
    assert "Priority: 🟠 HIGH" in message.text


# -- actual result -------------------------------------------------------------------

def test_actual_result_text_bls(sample):
    items, _, builder = sample
    item = pick(items, "CPI m/m", released=True)
    (message,) = [m for m in builder.actual_results(items) if m.event_id == item.event.event_id]
    assert message.text == """📊 *USD DATA RELEASED*
━━━━━━━━━━━━━━━━━━

📰 US CPI comes in above expectations

🇺🇸 *CPI m/m*

Previous: 0.4%
Forecast: 0.3%
Actual: *0.5%*

Result: 🔺 ABOVE FORECAST

Gold: 🟢 STRONG
Priority: 🔴 CRITICAL

━━━━━━━━━━━━━━━━━━
Source: BLS (calendar: Forex Factory)"""
    assert message.message_key == f"ACTUAL_{item.event.event_id}_1"
    assert (message.event_name, message.headline) == ("CPI m/m", "🇺🇸 US CPI comes in above expectations")
    assert message.attribution is None


@pytest.mark.parametrize("name, headline, result", [
    ("CPI m/m", "🇺🇸 US CPI comes in above expectations", "Result: 🔺 ABOVE FORECAST"),
    ("CPI y/y", "🇺🇸 US CPI comes in below expectations", "Result: 🔻 BELOW FORECAST"),
    ("Core CPI m/m", "🇺🇸 US core CPI in line with expectations", "Result: ➖ IN LINE WITH FORECAST"),
    ("Non-Farm Employment Change", "🇺🇸 US non-farm payrolls come in above expectations", "Result: 🔺 ABOVE FORECAST"),
    ("Unemployment Rate", "🇺🇸 US unemployment rate comes in below expectations", "Result: 🔻 BELOW FORECAST"),
    ("Average Hourly Earnings m/m", "🇺🇸 US average hourly earnings in line with expectations", "Result: ➖ IN LINE WITH FORECAST"),
])
def test_result_wording_for_above_below_and_in_line(sample, name, headline, result):
    items, _, builder = sample
    item = pick(items, name, released=True)
    (message,) = [m for m in builder.actual_results(items) if m.event_id == item.event.event_id]
    assert message.headline == headline and result in message.text
    assert f"*{name}*" in message.text and message.event_name == name


def test_fred_sourced_result_carries_the_required_notice(sample):
    items, _, builder = sample
    item = pick(items, "Core PCE Price Index m/m")
    (message,) = [m for m in builder.actual_results(items) if m.event_id == item.event.event_id]
    notice = "This product uses the FRED® API but is not endorsed or certified by the Federal Reserve Bank of St. Louis."
    assert message.text.endswith(f"Source: FRED (calendar: Forex Factory)\n_{notice}_")
    assert message.attribution == notice
    bls = [m for m in builder.actual_results(items) if "Source: BLS" in m.text]
    assert bls and all(m.attribution is None and "FRED" not in m.text for m in bls)


def test_no_comparison_is_invented_without_a_forecast(sample):
    items, _, builder = sample
    item = pick(items, "Core Retail Sales m/m")
    (message,) = [m for m in builder.actual_results(items) if m.event_id == item.event.event_id]
    assert "Result:" not in message.text and "FORECAST" not in message.text.replace("Forecast:", "")
    assert "Forecast: -\nActual: *0.4%*" in message.text
    assert message.headline == "🇺🇸 US core retail sales released"
    assert "\n\n\n" not in message.text  # the dropped line leaves no gap


def test_revised_figure_gets_its_own_key_and_note(sample):
    items, _, builder = sample
    item = pick(items, "PPI m/m")
    (message,) = [m for m in builder.actual_results(items) if m.event_id == item.event.event_id]
    assert message.message_key == f"ACTUAL_{item.event.event_id}_2"
    assert "🇺🇸 *PPI m/m*\nRevised figure (revision 2)" in message.text
    assert "Previous: -" in message.text  # missing previous
    first = replace(item, record=replace(item.record, actual_revision=1))
    (original,) = builder.actual_results([first])
    assert original.message_key == f"ACTUAL_{item.event.event_id}_1" and "Revised" not in original.text


def test_fed_decision_result(sample):
    items, _, builder = sample
    item = pick(items, "Federal Funds Rate", released=True)
    (message,) = [m for m in builder.actual_results(items) if m.event_id == item.event.event_id]
    assert message.headline == "🚨 Fed cuts interest rates to 4.00%"   # 4.25% -> 4.00%
    assert "📰 Fed cuts interest rates to 4.00%" in message.text and "🇺🇸 *Federal Funds Rate*" in message.text
    assert "Result: ➖ IN LINE WITH FORECAST" in message.text           # against the 4.00% forecast


def test_actual_messages_only_for_released_events(sample):
    items, _, builder = sample
    messages = builder.actual_results(items)
    assert len(messages) == 10
    released_ids = {i.event.event_id for i in items if i.record.release_status == "RELEASED"}
    assert {m.event_id for m in messages} == released_ids
    upcoming = pick(items, "CPI m/m", released=False)
    for status in (UPCOMING, NO_DATA, FAILED):
        fake = replace(upcoming, record=ActualRecord(upcoming.event.event_id, status, "x"))
        assert builder.actual_results([fake]) == []
    # RELEASED without a stored value is not trusted either.
    released = pick(items, "CPI m/m", released=True)
    assert builder.actual_results([replace(released, event=replace(released.event, actual=None))]) == []


# -- upcoming reminder ----------------------------------------------------------------

def test_upcoming_reminder_text(sample):
    items, _, builder = sample
    message = next(m for m in builder.upcoming_reminders(items) if m.event_name == "Federal Funds Rate")
    assert message.text == """⏰ *UPCOMING USD EVENT*
━━━━━━━━━━━━━━━━━━

📰 Fed interest-rate decision due on 14 Nov

🇺🇸 *Federal Funds Rate*

Release: 12:30 AM IST, Saturday 14 Nov 2026

Impact: 🔴 HIGH
Gold: 🟢 STRONG
Priority: 🔴 CRITICAL

Previous: 4.00%
Forecast: 4.00%

━━━━━━━━━━━━━━━━━━
Source: Forex Factory"""
    assert message.message_key.startswith("UPCOMING_ff-") and message.message_type == "UPCOMING_REMINDER"


def test_reminders_cover_upcoming_high_priority_events_only(template_config, sample):
    items, _, builder = sample
    assert [m.event_name for m in builder.upcoming_reminders(items)] == [
        "CPI m/m", "Core CPI m/m", "Federal Funds Rate", "FOMC Meeting Minutes", "Retail Sales m/m"]
    medium = custom_builder(template_config, sample, lambda c: c["selection"].update(upcoming_minimum_priority="MEDIUM"))
    assert "Unemployment Claims" in [m.event_name for m in medium.upcoming_reminders(items)]
    assert not any(m.event_name == "Non-Farm Employment Change" for m in medium.upcoming_reminders(items))  # already out


# -- headline versus event name ---------------------------------------------------------

def test_every_event_message_has_a_separate_headline_and_exact_event_name(sample):
    items, _, builder = sample
    by_id = {i.event.event_id: i.event.event_name for i in items}
    messages = builder.high_alerts(items) + builder.actual_results(items) + builder.upcoming_reminders(items)
    assert len(messages) == 4 + 10 + 5
    for m in messages:
        assert m.event_name == by_id[m.event_id]                 # exactly the stored name
        assert m.headline and m.headline != m.event_name
        assert m.event_name not in m.headline
        assert f"*{m.event_name}*" in m.text                     # the name is printed, verbatim
        headline_text = m.headline.split(" ", 1)[1]
        assert f"📰 {headline_text}" in m.text                   # and so is the headline, on its own line
        assert m.events == [{"event_id": m.event_id, "event_name": m.event_name, "headline": m.headline}]


def test_headline_changes_between_upcoming_and_released_but_the_name_stays(sample):
    items, _, builder = sample
    before, after = pick(items, "CPI m/m", released=False), pick(items, "CPI m/m", released=True)
    (alert,) = [m for m in builder.high_alerts(items) if m.event_id == before.event.event_id]
    (result,) = [m for m in builder.actual_results(items) if m.event_id == after.event.event_id]
    assert alert.headline == "🇺🇸 US CPI inflation data due today"
    assert result.headline == "🇺🇸 US CPI comes in above expectations"
    assert alert.event_name == result.event_name == "CPI m/m"


def test_builder_does_not_modify_its_input(sample):
    items, _, builder = sample
    snapshot = copy.deepcopy(items)
    builder.morning_update(items, DAY)
    builder.high_alerts(items), builder.actual_results(items), builder.upcoming_reminders(items)
    assert items == snapshot


# -- values, formatting, determinism ------------------------------------------------------

def test_variables_cover_every_documented_name(sample):
    items, _, builder = sample
    for item in items:
        values = builder.variables(item)
        assert set(values) == set(EVENT_VARIABLES)
        assert all(v is None or (isinstance(v, str) and v.strip()) for v in values.values())
        assert values["event_name"] == item.event.event_name


def test_values_of_a_released_event(sample):
    items, _, builder = sample
    v = builder.variables(pick(items, "Non-Farm Employment Change"))
    expected = {
        "event_name": "Non-Farm Employment Change", "currency": "USD", "currency_flag": "🇺🇸",
        "date": "06 Nov 2026", "weekday": "Friday", "time": "19:00", "display_time": "07:00 PM IST",
        "impact": "High", "impact_label": "🔴 HIGH", "gold_relevance": "YES", "gold_relevance_level": "STRONG",
        "gold_label": "🟢 STRONG", "category": "EMPLOYMENT", "category_label": "EMPLOYMENT", "priority": "CRITICAL",
        "priority_label": "🔴 CRITICAL", "priority_score": "100", "highlight_required": "YES",
        "forecast": "50K", "previous": "29K", "actual": "85K", "release_status": "RELEASED", "actual_source": "BLS",
        "actual_period": "2026-10", "actual_revision": "1", "surprise_status": "ABOVE FORECAST",
        "surprise_label": "🔺 ABOVE FORECAST", "surprise_value": "+35K", "source": "Forex Factory",
        "headline": "🇺🇸 US non-farm payrolls come in above expectations",
        "headline_text": "US non-farm payrolls come in above expectations",
    }
    assert {k: v[k] for k in expected} == expected
    assert v["revision_note"] is None and v["attribution"] is None


def test_values_of_an_upcoming_event_are_missing_not_invented(sample):
    items, _, builder = sample
    v = builder.variables(pick(items, "FOMC Meeting Minutes"))
    for name in ("forecast", "previous", "actual", "actual_source", "actual_period", "actual_revision",
                 "surprise_status", "surprise_label", "surprise_value", "attribution", "revision_note"):
        assert v[name] is None, name
    assert (v["release_status"], v["category_label"]) == ("UPCOMING", "FED COMMUNICATION")


def test_surprise_value_keeps_the_unit(sample):
    items, _, builder = sample
    assert builder.variables(pick(items, "Unemployment Rate"))["surprise_value"] == "-0.1 pts"
    assert builder.variables(pick(items, "Average Hourly Earnings m/m"))["surprise_value"] == "+0 pts"
    assert builder.variables(pick(items, "Core Retail Sales m/m"))["surprise_value"] is None


def test_no_message_ever_prints_a_raw_missing_marker(sample):
    items, _, builder = sample
    messages = [builder.morning_update(items, DAY), builder.morning_update(items, date(2026, 11, 6))]
    messages += builder.high_alerts(items) + builder.actual_results(items) + builder.upcoming_reminders(items)
    for m in messages:
        for marker in ("None", "null", "undefined", "N/A", "NOT_AVAILABLE", "{", "}"):
            assert marker not in m.text, (m.message_key, marker)


def test_numbers_are_shown_exactly_as_stored(sample):
    items, _, builder = sample
    text = "\n".join(m.text for m in builder.actual_results(items))
    for value in ("85K", "4.1%", "0.3%", "4.00%", "0.5%", "3.2%", "29K", "4.25%"):
        assert value in text
    odd = replace(pick(items, "30-y Bond Auction"), classification=pick(items, "CPI m/m", released=False).classification)
    assert "Previous: 4.83|2.4" in builder.upcoming_reminders([odd])[0].text  # untouched, not "converted"


def test_times_are_india_time_and_the_stored_instant_is_untouched(settings, sample):
    items, now, builder = sample
    cpi = pick(items, "CPI m/m", released=False)
    assert cpi.event.datetime_utc == "2026-11-12T13:30:00Z"
    assert builder.variables(cpi)["display_time"] == "07:00 PM IST"
    assert cpi.event.datetime_utc == "2026-11-12T13:30:00Z"
    # 19:00 UTC on the 13th is already the 14th in India: the date shown follows India time.
    fed = builder.variables(pick(items, "Federal Funds Rate", released=False))
    assert (fed["display_time"], fed["date"], fed["weekday"]) == ("12:30 AM IST", "14 Nov 2026", "Saturday")
    # The display timezone is configuration; the instant is the same.
    utc = build_content_builder(replace(settings, display_timezone=None), now)
    assert utc.variables(cpi)["display_time"] == "01:30 PM UTC"
    new_york = build_content_builder(replace(settings, display_timezone="America/New_York"), now)
    assert new_york.variables(cpi)["display_time"] == "08:30 AM EST"


def test_time_format_is_configurable(template_config, sample):
    items = sample[0]
    b = custom_builder(template_config, sample, lambda c: c["time"].update(time_format="%H:%M"))
    assert b.variables(pick(items, "CPI m/m", released=False))["display_time"] == "19:00 IST"


def test_output_is_deterministic(settings, sample):
    items, now, builder = sample
    def everything(b, data):
        return [m.to_dict() for m in [b.morning_update(data, DAY)] + b.high_alerts(data) + b.actual_results(data)
                + b.upcoming_reminders(data)]
    again_items, again_now = load_preview_fixture(settings)
    assert everything(builder, items) == everything(build_content_builder(settings, again_now), again_items)
    assert everything(builder, items) == everything(builder, items)
    # A later generation time changes generated_at only, never the text or the key.
    later = build_content_builder(settings, datetime(2026, 11, 12, 4, 0, tzinfo=timezone.utc))
    for a, b in zip(everything(builder, items), everything(later, items)):
        assert (a["text"], a["message_key"]) == (b["text"], b["message_key"]) and a["generated_at"] != b["generated_at"]


def test_message_keys(sample):
    items, _, builder = sample
    messages = [builder.morning_update(items, DAY)] + builder.high_alerts(items) + builder.actual_results(items) \
        + builder.upcoming_reminders(items)
    keys = [m.message_key for m in messages]
    assert len(set(keys)) == len(keys)
    assert keys[0] == "DAILY_UPDATE_2026-11-12"
    for m in messages[1:]:
        prefix = {"HIGH_ALERT": "HIGH_ALERT_", "ACTUAL_RESULT": "ACTUAL_", "UPCOMING_REMINDER": "UPCOMING_"}[m.message_type]
        assert m.message_key.startswith(prefix + m.event_id)
    assert all(k.replace("-", "").replace("_", "").isalnum() for k in keys)


def test_messages_are_markdown_safe_and_plain_enough_for_whatsapp(sample):
    items, _, builder = sample
    messages = [builder.morning_update(items, DAY)] + builder.high_alerts(items) + builder.actual_results(items) \
        + builder.upcoming_reminders(items)
    for m in messages:
        assert m.markdown_safe and markdown_safe(m.text)
        for forbidden in ("`", "[", "](", "<b>", "</", "__", "**", "~~", "||", "#"):
            assert forbidden not in m.text, (m.message_key, forbidden)
        assert max(len(line) for line in m.text.splitlines()) <= 120
        # Meaning never depends on the markers: without them the text still reads the same.
        plain = m.text.replace("*", "").replace("_", "")
        assert (m.event_name or "USD + GOLD DAILY UPDATE") in plain
    assert len(messages[0].text) < 1000  # the daily update stays short enough for a phone


def test_a_value_that_would_break_markdown_is_flagged_not_altered(sample):
    items, _, builder = sample
    odd = pick(items, "CPI m/m", released=False)
    odd = replace(odd, event=replace(odd.event, event_name="CPI_m/m *flash"))
    (message,) = builder.upcoming_reminders([odd])
    assert message.event_name == "CPI_m/m *flash" and "*CPI_m/m *flash*" in message.text   # shown exactly
    assert message.markdown_safe is False
    assert not markdown_safe("a *b") and not markdown_safe("a_b") and not markdown_safe("`x`") and markdown_safe("*a* _b_")


def test_no_trading_language_anywhere(sample, template_config):
    items, _, builder = sample
    text = "\n".join(m.text for m in [builder.morning_update(items, DAY)] + builder.high_alerts(items)
                     + builder.actual_results(items) + builder.upcoming_reminders(items)).casefold()
    text += json.dumps(template_config, ensure_ascii=False).casefold().replace("no trade direction", "")
    for word in ["bullish", "bearish", "buy", "sell", "long gold", "short gold", "will rise", "will fall",
                 "guaranteed", "explode", "target price", "stop loss", "trade"]:
        assert word not in text, word


# -- template configuration -----------------------------------------------------------------

def test_templates_can_be_edited_without_touching_python(template_config, sample):
    items = sample[0]

    def edit(config):
        config["templates"]["HIGH_ALERT"]["body"] = ["ALERT: {headline}", "{event_name} at {display_time} ({category_label})",
                                                     "Score {priority_score} | {gold_relevance} | {classification_reason}"]
        config["labels"]["priority"]["CRITICAL"] = "TOP"
        config["missing_value"] = "n/a"

    b = custom_builder(template_config, sample, edit)
    message = next(m for m in b.high_alerts(items) if m.event_name == "CPI m/m")
    assert message.text == ("ALERT: 🇺🇸 US CPI inflation data due today\nCPI m/m at 07:00 PM IST (INFLATION)\n"
                            "Score 100 | YES | Priority CRITICAL: score 100/100 = Forex Factory impact High (30) "
                            "+ Gold relevance STRONG (40) + critical event (30).")
    minutes = next(m for m in b.upcoming_reminders(items) if m.event_name == "FOMC Meeting Minutes")
    assert "Previous: n/a" in minutes.text


def test_omitted_lines_are_configurable(template_config, sample):
    items = sample[0]
    b = custom_builder(template_config, sample, lambda c: c.update(omit_line_if_missing=["forecast", "previous"]))
    minutes = next(m for m in b.upcoming_reminders(items) if m.event_name == "FOMC Meeting Minutes")
    assert "Previous" not in minutes.text and "Forecast" not in minutes.text and "\n\n\n" not in minutes.text


@pytest.mark.parametrize("mutate, message", [
    (lambda c: c.pop("templates"), "malformed"),
    (lambda c: c["templates"].pop("ACTUAL_RESULT"), "malformed"),
    (lambda c: c["templates"]["HIGH_ALERT"].update(body="one string"), "must be a list of text lines"),
    (lambda c: c["templates"]["HIGH_ALERT"]["body"].append("{gold_price}"), r"unknown placeholder\(s\) \{gold_price\}"),
    (lambda c: c["templates"]["HIGH_ALERT"]["body"].append("{unclosed"), "malformed line"),
    (lambda c: c["templates"]["MORNING_UPDATE"]["header"].append("{event_name}"), "unknown placeholder"),
    (lambda c: c["templates"]["ACTUAL_RESULT"].update(body=["{headline}", "{actual}"]), r"must include \{event_name\}"),
    (lambda c: c["templates"]["HIGH_ALERT"].update(body=["{event_name}"]), r"must include \{headline\}"),
    (lambda c: c["templates"]["MORNING_UPDATE"].update(event=["{headline}"]), r"must include \{event_name\}"),
    (lambda c: c["selection"].update(daily_minimum_priority="URGENT"), "daily_minimum_priority must be one of"),
    (lambda c: c["selection"].pop("upcoming_minimum_priority"), "malformed"),
    (lambda c: c["labels"]["priority"].pop("LOW"), "labels.priority has no text for: LOW"),
    (lambda c: c["labels"]["surprise_status"].update(ABOVE_FORECAST=""), "labels.surprise_status has no text"),
    (lambda c: c.update(omit_line_if_missing=["price"]), "omit_line_if_missing lists unknown"),
    (lambda c: c["labels"].update(revision_note="rev {number}"), "may only use"),
])
def test_broken_template_files_are_rejected(template_config, mutate, message):
    custom = copy.deepcopy(template_config)
    mutate(custom)
    with pytest.raises(TemplateConfigError, match=message):
        MessageTemplates(custom)


def test_unreadable_template_file(tmp_path):
    with pytest.raises(TemplateConfigError, match="Cannot read"):
        MessageTemplates.from_file(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("[]", encoding="utf-8")
    with pytest.raises(TemplateConfigError, match="JSON object"):
        MessageTemplates.from_file(bad)


def test_shipped_configuration_loads():
    templates = MessageTemplates.from_file(TEMPLATES_PATH)
    assert (templates.daily_minimum_priority, templates.upcoming_minimum_priority, templates.missing) == ("MEDIUM", "HIGH", "-")
    assert HeadlineRules.from_file(PROJECT_ROOT / "config" / "headline_rules.json").version == "1.0.0"
