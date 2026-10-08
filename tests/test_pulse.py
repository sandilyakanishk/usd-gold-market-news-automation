"""The half-hourly market pulse: gold price, change since the last post, next high-impact USD event."""

import json
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src import pulse
from src.collector.models import Event
from src.database.database import SQLiteRepository
from src.delivery.models import DeliveryError, PROVIDER_TELEGRAM

IST = ZoneInfo("Asia/Kolkata")
CHAT = "@example_channel"
SETTINGS = SimpleNamespace(gold_price_url="https://prices.example.test/XAU", request_timeout_seconds=5,
                           user_agent="test-agent", display_timezone="Asia/Kolkata")
# Thursday 8 October 2026, 16:04 India time = 10:34 UTC.
NOW = datetime(2026, 10, 8, 10, 34, tzinfo=timezone.utc)


class FakeTelegram:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send_text(self, chat_id, text, *, markdown=True):
        if self.fail:
            raise DeliveryError("Telegram is unavailable.")
        self.sent.append((chat_id, text, markdown))
        return str(100 + len(self.sent))


def quote(price=4114.40, age_minutes=1, now=NOW):
    return pulse.PriceQuote(price, now - timedelta(minutes=age_minutes), "prices.example.test")


def fetcher(*quotes):
    calls, queue = [], list(quotes)

    def fetch(url, *, timeout, user_agent):
        calls.append(url)
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item
    fetch.calls = calls
    return fetch


def event(name, utc, impact="High", currency="USD"):
    local = utc.astimezone(IST)
    stamp = "2026-10-08T00:00:00Z"
    return Event(
        event_id=f"ff-{abs(hash((name, utc.isoformat()))) % 10**12:012d}", date=local.date().isoformat(),
        time=local.strftime("%H:%M"), timezone="Asia/Kolkata", datetime_utc=utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        currency=currency, event_name=name, impact=impact, original_impact=impact, gold_relevance=True,
        forecast=None, previous=None, actual=None, source="forexfactory", source_url="https://example.test/feed",
        retrieved_at=stamp, updated_at=stamp)


@pytest.fixture
def db():
    with SQLiteRepository(":memory:") as repo:
        yield repo


# -- timing --------------------------------------------------------------------------------

@pytest.mark.parametrize("moment, expected", [
    (datetime(2026, 10, 8, 10, 34, tzinfo=timezone.utc), True),     # Thursday
    (datetime(2026, 10, 9, 20, 59, tzinfo=timezone.utc), True),     # Friday, just before the close
    (datetime(2026, 10, 9, 21, 0, tzinfo=timezone.utc), False),     # Friday close
    (datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc), False),    # Saturday
    (datetime(2026, 10, 11, 21, 59, tzinfo=timezone.utc), False),   # Sunday, before the open
    (datetime(2026, 10, 11, 22, 0, tzinfo=timezone.utc), True),     # Sunday open
    (datetime(2026, 10, 12, 0, 0, tzinfo=timezone.utc), True),      # Monday
])
def test_market_hours(moment, expected):
    assert pulse.market_is_open(moment) is expected
    # The answer does not depend on the timezone the moment is expressed in.
    assert pulse.market_is_open(moment.astimezone(IST)) is expected


def test_slots_are_half_hours_and_line_up_with_india_time():
    assert pulse.slot_start(NOW) == datetime(2026, 10, 8, 10, 30, tzinfo=timezone.utc)
    assert pulse.slot_start(NOW).astimezone(IST).strftime("%H:%M") == "16:00"
    assert pulse.message_key(pulse.slot_start(NOW)) == "MARKET_PULSE_2026-10-08T1030Z"
    keys = {pulse.message_key(pulse.slot_start(NOW.replace(hour=h, minute=m))) for h in range(24) for m in range(0, 60, 10)}
    assert len(keys) == 48                                           # a run every 10 minutes still gives 48 posts a day


# -- price source --------------------------------------------------------------------------

def test_parse_quote_reads_the_live_reply_format():
    body = json.dumps({"currency": "USD", "name": "Gold", "price": 4114.399902, "symbol": "XAU",
                       "updatedAt": "2026-10-08T10:34:37Z"})
    parsed = pulse.parse_quote(body, "prices.example.test")
    assert parsed.price == pytest.approx(4114.399902)
    assert parsed.updated_at == datetime(2026, 10, 8, 10, 34, 37, tzinfo=timezone.utc)


@pytest.mark.parametrize("body", [
    "", "<html>busy</html>", "[]", '{"price": "n/a", "updatedAt": "2026-10-08T10:34:37Z"}',
    '{"price": 0, "updatedAt": "2026-10-08T10:34:37Z"}', '{"price": -5, "updatedAt": "2026-10-08T10:34:37Z"}',
    '{"price": 4114.4}', '{"price": 4114.4, "updatedAt": "yesterday"}', '{"price": NaN, "updatedAt": "2026-10-08T10:34:37Z"}',
])
def test_parse_quote_rejects_anything_that_is_not_a_usable_price(body):
    with pytest.raises(pulse.PriceError):
        pulse.parse_quote(body, "prices.example.test")


# -- text ----------------------------------------------------------------------------------

@pytest.mark.parametrize("delta, expected", [
    (timedelta(seconds=20), "in 1m"), (timedelta(minutes=45), "in 45m"), (timedelta(hours=2), "in 2h 0m"),
    (timedelta(hours=3, minutes=20), "in 3h 20m"), (timedelta(days=2, hours=4, minutes=10), "in 2d 4h"),
])
def test_countdown(delta, expected):
    assert pulse.format_countdown(delta) == expected


def test_change_line_states_the_move_without_an_opinion():
    since = datetime(2026, 10, 8, 15, 30, tzinfo=IST)
    assert pulse.format_change(4114.40, 4111.20, since) == "🔺 +$3.20 (+0.08%) since 3:30 PM"
    assert pulse.format_change(4100.00, 4111.20, since) == "🔻 -$11.20 (-0.27%) since 3:30 PM"
    assert pulse.format_change(4111.204, 4111.20, since) == "▪️ Unchanged since 3:30 PM"


def test_full_text(db):
    db.upsert_events([event("Unemployment Claims", datetime(2026, 10, 8, 12, 30, tzinfo=timezone.utc))])
    previous = {"slot": "2026-10-08T10:00:00Z", "price": 4111.20}
    text = pulse.build_pulse_text(now=NOW, quote=quote(), previous=previous,
                                  upcoming=pulse.next_high_impact_event(db, NOW, IST), tz=IST)
    assert text == "\n".join([
        "🟡 *GOLD MARKET PULSE*",
        "🗓 Thu, 8 Oct · 4:04 PM IST",
        "",
        "💰 *XAU/USD: $4,114.40*",
        "🔺 +$3.20 (+0.08%) since 3:30 PM",
        "",
        "⏭ *Next high-impact USD event*",
        "Unemployment Claims",
        "🕒 6:00 PM IST today · in 1h 56m",
        "",
        pulse.DISCLAIMER,
    ])


def test_text_without_a_previous_price_or_an_upcoming_event():
    text = pulse.build_pulse_text(now=NOW, quote=quote(), previous=None, upcoming=None, tz=IST)
    assert "since" not in text
    assert "No high or medium-impact event left on this week's calendar." in text
    # A price from long ago is not compared with.
    old = {"slot": "2026-10-06T10:00:00Z", "price": 3900.0}
    assert "since" not in pulse.build_pulse_text(now=NOW, quote=quote(), previous=old, upcoming=None, tz=IST)


def test_the_pulse_never_reads_like_a_trading_signal(db):
    db.upsert_events([event("Core CPI m/m", datetime(2026, 10, 9, 12, 30, tzinfo=timezone.utc))])
    for previous_price in (4000.0, 4114.40, 4300.0):
        text = pulse.build_pulse_text(now=NOW, quote=quote(), previous={"slot": "2026-10-08T10:00:00Z", "price": previous_price},
                                      upcoming=pulse.next_high_impact_event(db, NOW, IST), tz=IST)
        assert not re.search(r"\b(buy|sell|bullish|bearish|long|short|target|will rise|will fall|rally|crash)\b", text, re.I)
        assert "not trading advice" in text


def test_next_event_is_the_nearest_future_high_impact_usd_one(db):
    db.upsert_events([
        event("Already Released", NOW - timedelta(hours=1)),
        event("Medium One", NOW + timedelta(minutes=30), impact="Medium"),
        event("Euro One", NOW + timedelta(minutes=40), currency="EUR"),
        event("FOMC Meeting Minutes", NOW + timedelta(days=1, hours=2)),
        event("Core CPI m/m", NOW + timedelta(hours=5)),
    ])
    instant, found = pulse.next_high_impact_event(db, NOW, IST)
    assert found.event_name == "Core CPI m/m" and instant == NOW + timedelta(hours=5)
    text = pulse.build_pulse_text(now=NOW + timedelta(hours=6), quote=quote(), previous=None,
                                  upcoming=pulse.next_high_impact_event(db, NOW + timedelta(hours=6), IST), tz=IST)
    assert "FOMC Meeting Minutes" in text and "tomorrow" in text


def test_a_week_with_no_high_impact_event_left_shows_the_next_medium_one(db):
    db.upsert_events([
        event("Natural Gas Storage", NOW + timedelta(minutes=20), impact="Low"),
        event("Unemployment Claims", NOW + timedelta(hours=1, minutes=56), impact="Medium"),
        event("Prelim UoM Consumer Sentiment", NOW + timedelta(days=1), impact="Medium"),
    ])
    instant, found = pulse.next_key_event(db, NOW, IST)
    assert found.event_name == "Unemployment Claims"
    text = pulse.build_pulse_text(now=NOW, quote=quote(), previous=None, upcoming=(instant, found), tz=IST)
    assert "⏭ *Next medium-impact USD event*\nUnemployment Claims\n🕒 6:00 PM IST today · in 1h 56m" in text
    # A high-impact event always wins, even when a medium one comes sooner.
    db.upsert_events([event("Core CPI m/m", NOW + timedelta(days=2))])
    assert pulse.next_key_event(db, NOW, IST)[1].event_name == "Core CPI m/m"
    # Low-impact events are never shown.
    assert pulse.next_key_event(db, NOW + timedelta(days=3), IST) is None


# -- sending -------------------------------------------------------------------------------

def test_a_slot_is_posted_once_however_often_the_automation_runs(db):
    client, fetch = FakeTelegram(), fetcher(quote())
    outcomes = [pulse.send_pulse(db, client, SETTINGS, CHAT, now=NOW + timedelta(minutes=m), fetch=fetch).outcome
                for m in (0, 10, 20)]                               # 16:04, 16:14, 16:24 India time: one slot
    assert outcomes == [pulse.OUTCOME_SENT, pulse.OUTCOME_ALREADY_SENT, pulse.OUTCOME_ALREADY_SENT]
    assert len(client.sent) == 1 and client.sent[0][0] == CHAT and client.sent[0][2] is True
    assert len(fetch.calls) == 1                                    # repeat runs do not ask the price source again
    assert db.get_delivery("MARKET_PULSE_2026-10-08T1030Z", PROVIDER_TELEGRAM, CHAT).message_type == "MARKET_PULSE"


def test_the_next_slot_reports_the_change_since_the_previous_post(db):
    client = FakeTelegram()
    later = NOW + timedelta(minutes=30)
    pulse.send_pulse(db, client, SETTINGS, CHAT, now=NOW, fetch=fetcher(quote(4111.20)))
    result = pulse.send_pulse(db, client, SETTINGS, CHAT, now=later, fetch=fetcher(quote(4114.40, now=later)))
    assert result.outcome == pulse.OUTCOME_SENT and len(client.sent) == 2
    assert "since" not in client.sent[0][1]
    assert "🔺 +$3.20 (+0.08%) since 4:00 PM" in client.sent[1][1]
    assert db.count_price_snapshots() == 2


def test_nothing_is_posted_over_the_weekend(db):
    client, fetch = FakeTelegram(), fetcher(quote())
    saturday = datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc)
    assert pulse.send_pulse(db, client, SETTINGS, CHAT, now=saturday, fetch=fetch).outcome == pulse.OUTCOME_MARKET_CLOSED
    assert client.sent == [] and fetch.calls == [] and db.count_deliveries() == 0


def test_a_price_that_stopped_updating_is_not_posted(db):
    client = FakeTelegram()
    result = pulse.send_pulse(db, client, SETTINGS, CHAT, now=NOW, fetch=fetcher(quote(age_minutes=120)))
    assert result.outcome == pulse.OUTCOME_STALE_PRICE
    assert client.sent == [] and db.count_deliveries() == 0 and db.count_price_snapshots() == 0


def test_a_price_source_failure_sends_nothing_and_the_next_run_retries(db):
    client = FakeTelegram()
    with pytest.raises(pulse.PriceError):
        pulse.send_pulse(db, client, SETTINGS, CHAT, now=NOW, fetch=fetcher(pulse.PriceError("down")))
    assert client.sent == [] and db.count_deliveries() == 0
    retry = pulse.send_pulse(db, client, SETTINGS, CHAT, now=NOW + timedelta(minutes=10), fetch=fetcher(quote()))
    assert retry.outcome == pulse.OUTCOME_SENT and len(client.sent) == 1


def test_a_failed_send_is_recorded_stores_no_price_and_is_retried(db):
    failed = pulse.send_pulse(db, FakeTelegram(fail=True), SETTINGS, CHAT, now=NOW, fetch=fetcher(quote()))
    assert failed.outcome == pulse.OUTCOME_FAILED and db.count_price_snapshots() == 0
    client = FakeTelegram()
    assert pulse.send_pulse(db, client, SETTINGS, CHAT, now=NOW + timedelta(minutes=10), fetch=fetcher(quote())).outcome == pulse.OUTCOME_SENT
    assert len(client.sent) == 1 and db.count_price_snapshots() == 1


def test_dry_run_sends_and_stores_nothing(db):
    result = pulse.send_pulse(db, None, SETTINGS, CHAT, now=NOW, dry_run=True, fetch=fetcher(quote()))
    assert result.outcome == pulse.OUTCOME_DRY_RUN and "XAU/USD: $4,114.40" in result.text
    assert db.count_deliveries() == 0 and db.count_price_snapshots() == 0


def test_old_prices_are_cleared_out(db):
    db.save_price_snapshot("XAU", "2026-09-01T10:00:00Z", 3900.0, "s", None, "2026-09-01T10:00:05Z")
    pulse.send_pulse(db, FakeTelegram(), SETTINGS, CHAT, now=NOW, fetch=fetcher(quote()))
    assert db.count_price_snapshots() == 1
    assert db.latest_price_snapshot("XAU", "2026-10-08T10:30:00Z") is None


# -- storage -------------------------------------------------------------------------------

def test_price_snapshots_keep_the_first_price_of_a_slot_and_find_the_latest_earlier_one(db):
    db.save_price_snapshot("XAU", "2026-10-08T10:00:00Z", 4111.2, "s", "2026-10-08T10:00:01Z", "2026-10-08T10:00:05Z")
    db.save_price_snapshot("XAU", "2026-10-08T10:00:00Z", 9999.0, "s", None, "2026-10-08T10:09:00Z")
    db.save_price_snapshot("XAU", "2026-10-08T09:30:00Z", 4100.0, "s", None, "2026-10-08T09:30:05Z")
    db.save_price_snapshot("XAG", "2026-10-08T10:00:00Z", 50.0, "s", None, "2026-10-08T10:00:05Z")
    latest = db.latest_price_snapshot("XAU", "2026-10-08T10:30:00Z")
    assert (latest["slot"], latest["price"]) == ("2026-10-08T10:00:00Z", 4111.2)
    assert db.latest_price_snapshot("XAU", "2026-10-08T10:00:00Z")["price"] == 4100.0
    assert db.latest_price_snapshot("XAU", "2026-10-08T09:30:00Z") is None
    assert db.delete_price_snapshots_before("XAU", "2026-10-08T10:00:00Z") == 1
    assert db.count_price_snapshots() == 2


# -- command line --------------------------------------------------------------------------

def test_cli_dry_run_prints_the_post_and_sends_nothing(monkeypatch, tmp_path, capsys):
    from src import main as cli
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "events.db"))
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "log.txt"))
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    monkeypatch.setattr(pulse, "market_is_open", lambda now: True)
    monkeypatch.setattr(pulse.send_pulse, "__kwdefaults__",
                        {**pulse.send_pulse.__kwdefaults__,
                         "fetch": lambda url, **_: pulse.PriceQuote(4114.40, datetime.now(timezone.utc), "prices.example.test")})
    assert cli.main(["--telegram-send-pulse", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "GOLD MARKET PULSE" in out and "XAU/USD: $4,114.40" in out


def test_cli_reports_a_price_source_failure_without_a_traceback(monkeypatch, tmp_path, capsys):
    from src import main as cli

    def broken(url, **_):
        raise pulse.PriceError("The gold price source (prices.example.test) answered HTTP 503.")
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "events.db"))
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "log.txt"))
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    monkeypatch.setattr(pulse, "market_is_open", lambda now: True)
    monkeypatch.setattr(pulse.send_pulse, "__kwdefaults__", {**pulse.send_pulse.__kwdefaults__, "fetch": broken})
    assert cli.main(["--telegram-send-pulse", "--dry-run"]) == 1
    err = capsys.readouterr().err
    assert "PRICE SOURCE ERROR" in err and "retried on the next run" in err and "Traceback" not in err
