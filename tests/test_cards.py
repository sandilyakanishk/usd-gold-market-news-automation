"""The daily timetable of scheduled cards, price recording, key levels and the daily recap."""

import re
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src import cards, pulse
from src.database.database import SQLiteRepository
from src.delivery.models import DeliveryError

IST = ZoneInfo("Asia/Kolkata")
CHAT = "@example_channel"
SETTINGS = SimpleNamespace(gold_price_url="https://prices.example.test/XAU", request_timeout_seconds=5,
                           user_agent="test-agent", display_timezone="Asia/Kolkata", fred_api_key=None)
THURSDAY, FRIDAY, SATURDAY, MONDAY = date(2026, 10, 8), date(2026, 10, 9), date(2026, 10, 10), date(2026, 10, 12)


def ist(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)


class FakeTelegram:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send_text(self, chat_id, text, *, markdown=True, link_preview=False):
        if self.fail:
            raise DeliveryError("Telegram is unavailable.")
        self.sent.append(text)
        return str(100 + len(self.sent))


def price(value):
    def fetch(url, *, timeout, user_agent):
        fetch.calls += 1
        return pulse.PriceQuote(value, datetime.now(timezone.utc), "prices.example.test")
    fetch.calls = 0
    return fetch


def fetch_at(value, moment):
    def fetch(url, *, timeout, user_agent):
        return pulse.PriceQuote(value, moment.astimezone(timezone.utc), "prices.example.test")
    return fetch


def run(db, client, moment, value=4100.0, **kwargs):
    return cards.send_scheduled(db, client, SETTINGS, CHAT, now=moment, fetch=fetch_at(value, moment), **kwargs)


def record_day(db, day, prices, start_hour=9):
    """Store one price every 10 minutes from start_hour (India time)."""
    for i, value in enumerate(prices):
        moment = ist(day, start_hour) + timedelta(minutes=10 * i)
        cards.record_price(db, pulse.PriceQuote(value, moment.astimezone(timezone.utc), "s"), moment)


@pytest.fixture
def db():
    with SQLiteRepository(":memory:") as repo:
        yield repo


# -- timetable -----------------------------------------------------------------------------

def test_a_weekday_has_one_post_an_hour_with_pulse_and_corner_alternating():
    slots = cards.slots_for(THURSDAY, IST)
    hourly = [(s.start.strftime("%H:%M"), s.kind) for s in slots if s.kind in (cards.PULSE, cards.CORNER)]
    assert hourly == [(f"{h:02d}:00", cards.PULSE if h % 2 else cards.CORNER) for h in range(9, 24)]
    # Each trader's corner comes exactly one hour after a pulse.
    pulses = {s.start for s in slots if s.kind == cards.PULSE}
    assert all(s.start - timedelta(hours=1) in pulses for s in slots if s.kind == cards.CORNER)
    assert [s.variant for s in slots if s.kind == cards.CORNER] == ["quiz", "rule", "fact", "myth", "quiz", "rule", "fact"]
    fixed = {s.kind: s.start.strftime("%H:%M") for s in slots if s.kind not in (cards.PULSE, cards.CORNER)}
    assert fixed == {cards.KEY_LEVELS: "08:30", cards.DRIVERS: "11:30", cards.LEARN: "15:30", cards.RECAP: "23:30"}
    assert len(slots) == 19 and len({s.message_key for s in slots}) == 19
    assert slots[0].start.strftime("%H:%M") == "08:30" and slots[-1].start.strftime("%H:%M") == "23:30"


def test_the_weekend_keeps_only_the_cards_that_do_not_need_a_market():
    for day in (SATURDAY, date(2026, 10, 11)):
        kinds = {s.kind for s in cards.slots_for(day, IST)}
        assert kinds == {cards.CORNER, cards.LEARN}
    assert cards.PULSE in {s.kind for s in cards.slots_for(MONDAY, IST)}


def test_quiet_hours_and_grace():
    assert [cards.due_slots(ist(THURSDAY, h, m), IST) for h, m in ((0, 0), (3, 0), (7, 59), (8, 29))] == [[], [], [], []]
    assert [s.kind for s in cards.due_slots(ist(THURSDAY, 8, 30), IST)] == [cards.KEY_LEVELS]
    assert [s.kind for s in cards.due_slots(ist(THURSDAY, 9, 0), IST)] == [cards.KEY_LEVELS, cards.PULSE]
    assert [s.kind for s in cards.due_slots(ist(THURSDAY, 9, 49), IST)] == [cards.PULSE]      # a late run still posts
    assert cards.due_slots(ist(THURSDAY, 9, 50), IST) == []                                    # too late: wait for the next
    assert [s.kind for s in cards.due_slots(ist(THURSDAY, 23, 59), IST)] == [cards.RECAP]      # never spills into tomorrow
    assert cards.due_slots(ist(FRIDAY, 0, 5), IST) == []


def test_a_run_every_ten_minutes_posts_every_card_exactly_once(db):
    record_day(db, date(2026, 10, 7), [4000 + i for i in range(80)])
    client = FakeTelegram()
    moment, posted = ist(THURSDAY, 0, 3), []
    while moment.date() == THURSDAY:
        for result in run(db, client, moment):
            if result.outcome == cards.OUTCOME_SENT:
                posted.append((moment.strftime("%H:%M"), result.kind))
        moment += timedelta(minutes=10)
    # Pulse, key levels and recap are built here; the others arrive in a later part.
    assert [kind for _, kind in posted].count(cards.PULSE) == 8
    assert [t for t, kind in posted if kind == cards.PULSE] == [f"{h:02d}:03" for h in cards.PULSE_HOURS]
    assert [(t, k) for t, k in posted if k != cards.PULSE] == [("08:33", cards.KEY_LEVELS), ("23:33", cards.RECAP)]
    assert len(client.sent) == 10


# -- price recording -----------------------------------------------------------------------

def test_every_run_records_the_price_but_only_while_the_market_is_open(db):
    for minute in (0, 10, 20):
        run(db, FakeTelegram(), ist(THURSDAY, 3, minute))             # quiet hours: recorded, nothing posted
    assert db.count_price_snapshots() == 3 and db.count_deliveries() == 0
    run(db, FakeTelegram(), ist(THURSDAY, 3, 25))                      # same 10-minute slot: not recorded twice
    assert db.count_price_snapshots() == 3
    fetch = price(4100.0)
    cards.send_scheduled(db, FakeTelegram(), SETTINGS, CHAT, now=ist(SATURDAY, 7), fetch=fetch)
    assert fetch.calls == 0 and db.count_price_snapshots() == 3        # weekend: the source is not even asked


def test_a_price_that_stopped_updating_is_not_recorded_or_posted(db):
    stale = lambda url, **_: pulse.PriceQuote(4100.0, ist(THURSDAY, 6).astimezone(timezone.utc), "s")
    results = cards.send_scheduled(db, FakeTelegram(), SETTINGS, CHAT, now=ist(THURSDAY, 9, 5), fetch=stale)
    assert db.count_price_snapshots() == 0
    assert [(r.kind, r.outcome) for r in results if r.kind == cards.PULSE] == [(cards.PULSE, cards.OUTCOME_NOT_READY)]


def test_a_price_source_failure_does_not_stop_the_other_cards(db):
    record_day(db, date(2026, 10, 7), [4000 + i for i in range(80)])

    def broken(url, **_):
        raise pulse.PriceError("The gold price source answered HTTP 503.")
    client = FakeTelegram()
    results = cards.send_scheduled(db, client, SETTINGS, CHAT, now=ist(THURSDAY, 9, 5), fetch=broken)
    outcomes = {r.kind: r.outcome for r in results}
    assert outcomes["PRICE"] == cards.OUTCOME_FAILED and outcomes[cards.KEY_LEVELS] == cards.OUTCOME_SENT
    assert outcomes[cards.PULSE] == cards.OUTCOME_NOT_READY and len(client.sent) == 1


def test_past_days_become_one_summary_line_and_their_samples_are_deleted(db):
    record_day(db, date(2026, 10, 7), [4000, 4010, 3990, 4025, 4005, 4015, 4020])
    record_day(db, THURSDAY, [4020, 4030])
    assert cards.roll_prices(db, ist(THURSDAY, 9), IST) == 7
    assert db.count_price_snapshots() == 2                              # today's samples stay
    assert db.latest_daily_price("XAU", THURSDAY.isoformat()) == {
        "symbol": "XAU", "day": "2026-10-07", "open": 4000.0, "high": 4025.0, "low": 3990.0, "close": 4020.0, "samples": 7}
    assert cards.roll_prices(db, ist(THURSDAY, 12), IST) == 0


def test_a_day_with_too_few_samples_gets_no_summary(db):
    record_day(db, date(2026, 10, 7), [4000, 4010, 4020])
    cards.roll_prices(db, ist(THURSDAY, 9), IST)
    assert db.latest_daily_price("XAU", THURSDAY.isoformat()) is None and db.count_price_snapshots() == 0


def test_samples_are_only_rolled_up_from_the_morning_on(db):
    record_day(db, date(2026, 10, 7), [4000 + i for i in range(10)])
    run(db, FakeTelegram(), ist(THURSDAY, 7, 0))
    assert db.count_price_snapshots() == 11                              # still there before 08:15
    run(db, FakeTelegram(), ist(THURSDAY, 8, 20))
    assert db.count_price_snapshots() == 2 and db.latest_daily_price("XAU", THURSDAY.isoformat())["samples"] == 10


def test_old_summaries_are_dropped(db):
    db.save_daily_price("XAU", "2026-09-20", 1, 2, 0.5, 1.5, 50)
    db.save_daily_price("XAU", "2026-10-07", 1, 2, 0.5, 1.5, 50)
    cards.roll_prices(db, ist(THURSDAY, 9), IST)
    assert db.latest_daily_price("XAU", "2026-10-01") is None and db.latest_daily_price("XAU", "2026-10-08")["day"] == "2026-10-07"


# -- key levels ----------------------------------------------------------------------------

def test_pivot_arithmetic():
    levels = cards.pivot_levels(high=4130.0, low=4090.0, close=4110.0)
    assert levels == {"R2": 4150.0, "R1": 4130.0, "P": 4110.0, "S1": 4090.0, "S2": 4070.0}


def test_key_levels_text():
    daily = {"day": "2026-10-08", "open": 4101.2, "high": 4131.5, "low": 4094.2, "close": 4113.0}
    assert cards.build_key_levels(FRIDAY, daily) == "\n".join([
        "🗺 *KEY LEVELS* · Fri, 9 Oct",
        "XAU/USD · from Thursday's range",
        "",
        "```",
        "🔴 R2     4,150.20",
        "🟠 R1     4,131.60",
        "⚪ Pivot  4,112.90",
        "🟢 S1     4,094.30",
        "🟢 S2     4,075.60",
        "```",
        "Thursday: High 4,131.50 · Low 4,094.20 · Close 4,113.00",
        "",
        "ℹ️ Levels are arithmetic on recorded prices, not trading advice.",
    ])


def test_key_levels_use_the_last_trading_day_and_wait_if_there_is_none(db):
    client = FakeTelegram()
    first = run(db, client, ist(MONDAY, 8, 35))
    assert [(r.kind, r.outcome) for r in first] == [(cards.KEY_LEVELS, cards.OUTCOME_NOT_READY)] and client.sent == []
    db.save_daily_price("XAU", "2026-10-09", 4100, 4130, 4090, 4110, 80)      # Friday
    assert run(db, client, ist(MONDAY, 8, 45))[0].outcome == cards.OUTCOME_SENT
    assert "from Friday's range" in client.sent[0] and "Mon, 12 Oct" in client.sent[0]
    # A day that was only partly recorded is not presented as that day's range.
    with SQLiteRepository(":memory:") as partial:
        partial.save_daily_price("XAU", "2026-10-09", 4100, 4130, 4090, 4110, 30)
        assert run(partial, FakeTelegram(), ist(MONDAY, 8, 35))[0].outcome == cards.OUTCOME_NOT_READY
    # A summary from long ago is not presented as "the" key levels.
    with SQLiteRepository(":memory:") as other:
        other.save_daily_price("XAU", "2026-10-01", 4100, 4130, 4090, 4110, 80)
        assert run(other, FakeTelegram(), ist(MONDAY, 8, 35))[0].outcome == cards.OUTCOME_NOT_READY


# -- daily recap ---------------------------------------------------------------------------

def test_recap_text():
    summary = {"open": 4101.2, "high": 4131.5, "low": 4094.2, "close": 4113.0, "samples": 90}
    assert cards.build_recap(THURSDAY, summary) == "\n".join([
        "📊 *DAILY RECAP* · Thu, 8 Oct",
        "XAU/USD",
        "",
        "```",
        "Open  $4,101.20",
        "High  $4,131.50",
        "Low   $4,094.20",
        "Last  $4,113.00",
        "```",
        "🔺 +$11.80 (+0.29%) on the day",
        "📏 Day's range: $37.30",
        "",
        cards.NOTE,
    ])
    down = cards.build_recap(THURSDAY, {**summary, "close": 4090.0})
    assert "🔻 -$11.20 (-0.27%) on the day" in down
    assert "▪️ Unchanged on the day" in cards.build_recap(THURSDAY, {**summary, "close": 4101.2})


def test_recap_uses_todays_recorded_prices_only(db):
    record_day(db, date(2026, 10, 7), [9000 + i for i in range(10)], start_hour=20)     # yesterday: must not leak in
    record_day(db, THURSDAY, [4100, 4120, 4080, 4110, 4105, 4115, 4125], start_hour=10)
    client = FakeTelegram()
    results = run(db, client, ist(THURSDAY, 23, 55), value=4118.0)
    assert [(r.kind, r.outcome) for r in results] == [(cards.RECAP, cards.OUTCOME_SENT)]
    text = client.sent[0]
    assert "Open  $4,100.00" in text and "High  $4,125.00" in text and "Low   $4,080.00" in text and "Last  $4,118.00" in text
    assert "9,0" not in text


def test_recap_waits_when_too_little_was_recorded(db):
    client = FakeTelegram()
    assert run(db, client, ist(THURSDAY, 23, 55))[0].outcome == cards.OUTCOME_NOT_READY and client.sent == []


# -- pulse on the timetable ----------------------------------------------------------------

def test_the_pulse_compares_with_the_price_two_hours_earlier(db):
    client = FakeTelegram()
    run(db, client, ist(THURSDAY, 9, 2), value=4100.0)
    for minute in range(10, 120, 10):
        run(db, client, ist(THURSDAY, 9, 2) + timedelta(minutes=minute), value=4100.0 + minute / 10)
    run(db, client, ist(THURSDAY, 11, 2), value=4125.5)
    first, second = client.sent[0], client.sent[1]
    assert "GOLD MARKET PULSE" in first and "since" not in first
    assert "🔺 +$25.50 (+0.62%) since 9:00 AM" in second and "XAU/USD: $4,125.50" in second
    assert db.get_delivery("MARKET_PULSE_2026-10-08_1100", "telegram", CHAT) is not None


def test_no_card_reads_like_a_trading_signal(db):
    record_day(db, date(2026, 10, 7), [4000 + 3 * i for i in range(80)])
    record_day(db, THURSDAY, [4100 + i for i in range(20)], start_hour=10)
    client = FakeTelegram()
    for hour, minute in ((8, 35), (13, 5), (23, 55)):
        run(db, client, ist(THURSDAY, hour, minute))
    assert len(client.sent) == 3
    for text in client.sent:
        assert not re.search(r"\b(buy|sell|bullish|bearish|long|short|target|will rise|will fall|entry|stop loss)\b", text, re.I)
        assert "not trading advice" in text


# -- failures and dry run ------------------------------------------------------------------

def test_a_failed_post_is_retried_within_the_grace_period(db):
    assert [r.outcome for r in run(db, FakeTelegram(fail=True), ist(THURSDAY, 9, 2)) if r.kind == cards.PULSE] == [cards.OUTCOME_FAILED]
    client = FakeTelegram()
    assert [r.outcome for r in run(db, client, ist(THURSDAY, 9, 12)) if r.kind == cards.PULSE] == [cards.OUTCOME_SENT]
    assert len(client.sent) == 1


def test_dry_run_sends_and_stores_nothing(db):
    results = cards.send_scheduled(db, None, SETTINGS, CHAT, now=ist(THURSDAY, 9, 2), dry_run=True,
                                   fetch=fetch_at(4100.0, ist(THURSDAY, 9, 2)))
    assert [(r.kind, r.outcome) for r in results if r.kind == cards.PULSE] == [(cards.PULSE, cards.OUTCOME_DRY_RUN)]
    assert db.count_deliveries() == 0 and db.count_price_snapshots() == 0


def test_cards_built_elsewhere_plug_in_by_kind(db):
    seen = []

    def corner(slot):
        seen.append((slot.kind, slot.variant, slot.message_key))
        return cards.CardResult(slot.kind, cards.OUTCOME_SENT, slot.message_key)
    results = run(db, FakeTelegram(), ist(SATURDAY, 10, 4), extra_cards={cards.CORNER: corner})
    assert seen == [(cards.CORNER, "quiz", "TRADER_CORNER_2026-10-10_1000")]
    assert [(r.kind, r.outcome) for r in results] == [(cards.CORNER, cards.OUTCOME_SENT)]


def test_cli_dry_run(monkeypatch, tmp_path, capsys):
    from src import main as cli
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "events.db"))
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "log.txt"))
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    monkeypatch.setenv("FRED_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setattr(cards, "clock", lambda: ist(THURSDAY, 9, 2))
    monkeypatch.setattr(cards.send_scheduled, "__kwdefaults__",
                        {**cards.send_scheduled.__kwdefaults__, "fetch": fetch_at(4100.0, ist(THURSDAY, 9, 2))})
    assert cli.main(["--telegram-send-scheduled", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "MARKET_PULSE_2026-10-08_0900" in out and "GOLD MARKET PULSE" in out
    assert "KEY_LEVELS_2026-10-08_0830: skipped" in out
