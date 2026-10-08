"""Trader's corner and learn cards, the AI writer with its checks, what's moving gold, and the news countdown."""

import io
import json
import re
import urllib.error
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src import cards, content_cards as cc, pulse
from src.actuals.providers import ProviderError
from src.collector.models import Event
from src.config import PROJECT_ROOT
from src.database.database import SQLiteRepository
from src.delivery.models import DeliveryError
from src.delivery.telegram import TelegramClient, TelegramMessageError

IST = ZoneInfo("Asia/Kolkata")
CHAT = "@example_channel"
KEY = "AIza-TEST-GEMINI-KEY-0000000000000000000"
LIBRARY = cc.load_library(PROJECT_ROOT / "config" / "content_library.json")
THURSDAY, SATURDAY = date(2026, 10, 8), date(2026, 10, 10)


def settings(**changes):
    base = dict(gold_price_url="https://prices.example.test/XAU", request_timeout_seconds=5, user_agent="test-agent",
                display_timezone="Asia/Kolkata", fred_api_key=None, gemini_api_key=None, gemini_model="test-model")
    return SimpleNamespace(**{**base, **changes})


def ist(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)


class FakeTelegram:
    def __init__(self, fail=False):
        self.texts, self.quizzes, self.fail = [], [], fail

    def send_text(self, chat_id, text, *, markdown=True, link_preview=False):
        if self.fail:
            raise DeliveryError("Telegram is unavailable.")
        self.texts.append(text)
        return str(100 + len(self.texts))

    def send_quiz(self, chat_id, question, options, correct, explanation=""):
        if self.fail:
            raise DeliveryError("Telegram is unavailable.")
        self.quizzes.append((question, options, correct, explanation))
        return str(200 + len(self.quizzes))


def slot(kind, variant, day=THURSDAY, hour=10):
    return cards.Slot(kind, ist(day, hour), variant)


def post(db, client, config, kind_slot, **kwargs):
    return cc.send_content_card(db, client, config, CHAT, kind_slot, LIBRARY, now=kind_slot.start, **kwargs)


@pytest.fixture
def db():
    with SQLiteRepository(":memory:") as repo:
        yield repo


GOOD = {
    "quiz": {"question": "What does a central bank do?", "options": ["Sets interest rates", "Mines gold", "Sells phones", "Builds roads"],
             "answer": 0, "explanation": "Central banks set interest rate policy for their economy."},
    "rule": {"title": "Plan the exit first", "text": "Decide where you are wrong before entering, while you are still calm and clear."},
    "fact": {"text": "Gold does not corrode, which is one reason it has been used as money for thousands of years."},
    "myth": {"myth": "Trading is a quick way to get income.", "truth": "It is a skill that takes long practice, and most beginners lose money at first."},
    "lesson": {"title": "What is liquidity?", "points": ["Liquidity is how easily something can be bought or sold.",
               "A liquid market has many buyers and sellers at once.", "Thin liquidity leads to wider spreads and jumpy prices."],
               "takeaway": "More liquidity usually means smoother trading."},
}


# -- the library ---------------------------------------------------------------------------

def test_every_library_item_passes_the_same_checks_as_ai_text():
    assert {k: len(v) for k, v in LIBRARY.items()} == {"quiz": 24, "rule": 24, "fact": 24, "myth": 16, "lesson": 20}
    for kind, items in LIBRARY.items():
        headlines = [cc.headline(kind, i) for i in items]
        assert len(set(headlines)) == len(headlines), kind              # no duplicates
        for item in items:
            assert cc.validate(kind, item) == item
            # Telegram's own limits: 300 for a quiz question, 100 per option, 200 for the explanation, 4096 for text.
            assert len(cc.build_text(kind, item)) < 1500
    for quiz in LIBRARY["quiz"]:
        assert len(quiz["question"]) + len("❓ Quiz: ") <= 300 and all(len(o) <= 100 for o in quiz["options"])
        assert len(quiz["explanation"]) <= 200


def test_no_library_item_gives_a_trading_call():
    for kind, items in LIBRARY.items():
        for item in items:
            text = json.dumps(item)
            assert not re.search(r"\b(buy now|sell now|will rise|will fall|bullish|bearish|price target|guaranteed profit)\b", text, re.I), text[:80]


def test_a_broken_library_is_refused(tmp_path):
    bad = tmp_path / "library.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(cc.ContentError):
        cc.load_library(bad)
    bad.write_text(json.dumps({**{k: [GOOD[k]] for k in cc.KINDS}, "fact": [{"text": "Gold will rise to a price target of $9,000 soon, buy now."}]}), encoding="utf-8")
    with pytest.raises(cc.ContentError, match="fact"):
        cc.load_library(bad)
    bad.write_text(json.dumps({k: [GOOD[k]] for k in cc.KINDS if k != "myth"}), encoding="utf-8")
    with pytest.raises(cc.ContentError, match="myth"):
        cc.load_library(bad)


# -- checks --------------------------------------------------------------------------------

@pytest.mark.parametrize("kind", cc.KINDS)
def test_good_items_pass(kind):
    assert cc.validate(kind, GOOD[kind]) == GOOD[kind]


@pytest.mark.parametrize("text", [
    "Gold will rise sharply after the next Fed meeting, so be ready.", "You should buy gold whenever the dollar weakens a little.",
    "This simple method gives a guaranteed profit on every single trade.", "Time to sell: the chart says gold will fall from here.",
    "Join our group for daily signals at https://example.com today.", "Message @goldsignals for a risk-free strategy that works.",
    "Gold is currently trading at 4,100 dollars and this week it moves.", "A price target of $5,000 is what the experts are saying.",
    "With this trick you can double your account in a single month.",
])
def test_advice_predictions_and_promotion_are_rejected(text):
    with pytest.raises(cc.ContentError, match="advice, a prediction or a promotion"):
        cc.validate("fact", {"text": text})
    with pytest.raises(cc.ContentError):
        cc.validate("lesson", {**GOOD["lesson"], "takeaway": text})


def test_explaining_buying_and_selling_is_allowed():
    cc.validate("lesson", {"title": "Bid and ask", "points": ["The bid is the price at which you can sell to the market.",
                "The ask is the price at which you can buy from it.", "A stop-loss closes a trade to limit a loss."], "takeaway": "Know both prices before you trade."})


@pytest.mark.parametrize("kind, broken", [
    ("quiz", {**GOOD["quiz"], "options": ["A", "B", "C"]}), ("quiz", {**GOOD["quiz"], "answer": 4}),
    ("quiz", {**GOOD["quiz"], "answer": "0"}), ("quiz", {**GOOD["quiz"], "answer": True}),
    ("quiz", {**GOOD["quiz"], "options": ["Same", "same", "C", "D"]}), ("quiz", {**GOOD["quiz"], "question": "x" * 400}),
    ("quiz", {**GOOD["quiz"], "explanation": "x" * 300}), ("rule", {"title": "T", "text": "ok"}), ("fact", {"text": 5}),
    ("fact", "just a string"), ("myth", {"myth": GOOD["myth"]["myth"]}), ("lesson", {**GOOD["lesson"], "points": ["only one point here"]}),
    ("poem", {"text": "roses are red"}),
])
def test_wrong_shapes_are_rejected(kind, broken):
    with pytest.raises(cc.ContentError):
        cc.validate(kind, broken)


def test_markup_is_stripped_so_it_cannot_break_the_formatting():
    cleaned = cc.validate("fact", {"text": "Gold is *very* _dense_ and `soft`, [really] among the densest common metals."})
    assert cleaned["text"] == "Gold is very dense and soft, really among the densest common metals."


# -- text ----------------------------------------------------------------------------------

def test_each_kind_has_its_own_look():
    looks = {kind: cc.build_text(kind, GOOD[kind]) for kind in cc.KINDS}
    assert looks["rule"] == ("🛡 *TRADER'S RULE*\n\n*Plan the exit first*\nDecide where you are wrong before entering, "
                             "while you are still calm and clear.\n\n_Protect the account first._")
    assert looks["fact"].startswith("💡 *DID YOU KNOW?*\n\n")
    assert looks["myth"] == ("⚖️ *MYTH OR FACT?*\n\n❌ *Myth:* Trading is a quick way to get income.\n\n"
                             "✅ *Truth:* It is a skill that takes long practice, and most beginners lose money at first.")
    assert looks["lesson"].startswith("📘 *LEARN: WHAT IS LIQUIDITY?*\n\n1️⃣ Liquidity is how") and "🎯 *Remember:*" in looks["lesson"]
    assert looks["quiz"] == "❓ Quiz: What does a central bank do?"
    assert len({text.split("\n")[0] for text in looks.values()}) == 5


def test_quiz_options_are_rearranged_but_the_right_answer_stays_right():
    positions = set()
    for quiz in LIBRARY["quiz"]:
        options, correct = cc.arrange_quiz(quiz)
        assert sorted(options) == sorted(quiz["options"]) and options[correct] == quiz["options"][quiz["answer"]]
        assert cc.arrange_quiz(quiz) == (options, correct)               # always the same for the same question
        positions.add(correct)
    assert positions == {0, 1, 2, 3}


# -- taking items from the library ---------------------------------------------------------

def test_the_library_is_used_in_order_and_never_repeats_until_it_is_finished(db):
    client, used = FakeTelegram(), []
    for day in range(30):
        result = post(db, client, settings(), cards.Slot(cards.CORNER, ist(THURSDAY, 12) + timedelta(days=day), "rule"))
        assert result.outcome == cards.OUTCOME_SENT and result.detail == "rule from the library"
        used.append(client.texts[-1])
    titles = [f"*{item['title']}*" for item in LIBRARY["rule"]]
    assert all(titles[i % 24] in used[i] for i in range(30))            # in order, then round again
    assert len(set(used[:24])) == 24


def test_each_kind_has_a_bookmark_of_its_own(db):
    client = FakeTelegram()
    post(db, client, settings(), slot(cards.CORNER, "fact", hour=14))
    post(db, client, settings(), slot(cards.CORNER, "rule", hour=12))
    post(db, client, settings(), slot(cards.CORNER, "fact", THURSDAY + timedelta(days=1), 14))
    assert LIBRARY["fact"][0]["text"] in client.texts[0] and LIBRARY["rule"][0]["title"] in client.texts[1]
    assert LIBRARY["fact"][1]["text"] in client.texts[2]
    assert db.get_content_state("fact")["position"] == 2 and db.get_content_state("rule")["position"] == 1


def test_the_quiz_is_a_real_poll_and_the_learn_card_is_a_lesson(db):
    client = FakeTelegram()
    assert post(db, client, settings(), slot(cards.CORNER, "quiz")).outcome == cards.OUTCOME_SENT
    question, options, correct, explanation = client.quizzes[0]
    first = LIBRARY["quiz"][0]
    assert question == "❓ Quiz: " + first["question"] and options[correct] == first["options"][first["answer"]]
    assert explanation == first["explanation"] and client.texts == []
    assert post(db, client, settings(), cards.Slot(cards.LEARN, ist(THURSDAY, 15, 30))).outcome == cards.OUTCOME_SENT
    assert client.texts[0].startswith("📘 *LEARN: WHAT IS XAU/USD?*")


def test_a_failed_post_does_not_move_the_bookmark(db):
    assert post(db, FakeTelegram(fail=True), settings(), slot(cards.CORNER, "rule", hour=12)).outcome == cards.OUTCOME_FAILED
    assert db.get_content_state("rule") is None
    client = FakeTelegram()
    post(db, client, settings(), slot(cards.CORNER, "rule", hour=12))
    assert LIBRARY["rule"][0]["title"] in client.texts[0]              # the same item, not the next one


def test_dry_run_shows_the_card_and_changes_nothing(db):
    result = post(db, None, settings(), slot(cards.CORNER, "quiz"), dry_run=True)
    assert result.outcome == cards.OUTCOME_DRY_RUN and "✔" in result.text and "❓ Quiz:" in result.text
    assert db.get_content_state("quiz") is None and db.count_deliveries() == 0


# -- the AI writer -------------------------------------------------------------------------

def writer(*replies):
    calls = []

    def ask(prompt, api_key, **options):
        calls.append((prompt, api_key, options))
        reply = replies[min(len(calls), len(replies)) - 1]
        if isinstance(reply, Exception):
            raise reply
        return reply
    ask.calls = calls
    return ask


def test_with_a_key_the_ai_writes_the_card_and_the_bookmark_stays(db):
    client, ask = FakeTelegram(), writer(GOOD["fact"])
    result = post(db, client, settings(gemini_api_key=KEY), slot(cards.CORNER, "fact", hour=14), ask=ask)
    assert result.outcome == cards.OUTCOME_SENT and result.detail == "fact from the ai"
    assert GOOD["fact"]["text"] in client.texts[0]
    prompt, key, options = ask.calls[0]
    assert key == KEY and options["model"] == "test-model" and KEY not in prompt
    assert "ONE new fact" in prompt and cc.TOPICS[0] in prompt and "Never tell the reader to buy or sell" in prompt
    assert db.get_content_state("fact")["position"] == 0                # the library is untouched, kept as backup


@pytest.mark.parametrize("bad_reply", [
    cc.ContentError("the AI writer answered HTTP 429"), {"text": "Gold will rise next month, so buy now before it is too late."},
    {"wrong": "shape"}, {"text": "short"},
])
def test_if_the_ai_fails_or_writes_something_unfit_the_library_item_goes_out(db, bad_reply):
    client = FakeTelegram()
    result = post(db, client, settings(gemini_api_key=KEY), slot(cards.CORNER, "fact", hour=14), ask=writer(bad_reply))
    assert result.outcome == cards.OUTCOME_SENT and result.detail == "fact from the library"
    assert LIBRARY["fact"][0]["text"] in client.texts[0] and "buy now" not in client.texts[0]


def test_the_ai_is_told_what_was_used_lately_and_a_repeat_is_refused(db):
    client, config = FakeTelegram(), settings(gemini_api_key=KEY)
    post(db, client, config, slot(cards.CORNER, "fact", hour=14), ask=writer(GOOD["fact"]))
    ask = writer(GOOD["fact"])                                          # the AI repeats itself
    post(db, client, config, slot(cards.CORNER, "fact", hour=22), ask=ask)
    assert GOOD["fact"]["text"][:60] in ask.calls[0][0] and "used recently" in ask.calls[0][0]
    assert cc.TOPICS[1] in ask.calls[0][0]                              # and it is moved on to the next subject
    assert LIBRARY["fact"][0]["text"] in client.texts[1]                # the repeat was not posted


def test_topics_rotate_so_the_writer_covers_the_whole_field(db):
    client, config, seen = FakeTelegram(), settings(gemini_api_key=KEY), []
    for i in range(5):
        ask = writer({"text": f"Fact number {i}: gold has been valued by people for a very long time indeed."})
        post(db, client, config, cards.Slot(cards.CORNER, ist(THURSDAY, 14) + timedelta(days=i), "fact"), ask=ask)
        seen.append(next(t for t in cc.TOPICS if f"subject: {t}." in ask.calls[0][0]))
    assert seen == list(cc.TOPICS[:5]) and len(cc.TOPICS) >= 60 and len(set(cc.TOPICS)) == len(cc.TOPICS)


def test_models_are_tried_in_order_until_one_answers(db):
    """Google retires model names and its free models are sometimes busy."""
    tried = []

    def ask(prompt, api_key, *, model, **options):
        tried.append(model)
        if model != "third":
            raise cc.ContentError(f"the AI writer answered HTTP {503 if model == 'first' else 404}")
        return GOOD["fact"]
    client, config = FakeTelegram(), settings(gemini_api_key=KEY, gemini_model="first, second ,third,fourth")
    result = post(db, client, config, slot(cards.CORNER, "fact", hour=14), ask=ask)
    assert tried == ["first", "second", "third"] and result.detail == "fact from the ai"

    def all_down(prompt, api_key, *, model, **options):
        raise cc.ContentError("the AI writer answered HTTP 503")
    again = post(db, client, config, slot(cards.CORNER, "fact", hour=22), ask=all_down)
    assert again.outcome == cards.OUTCOME_SENT and again.detail == "fact from the library"


def test_only_recent_items_are_remembered(db):
    state = {"position": 3, "recent": [f"item {i}" for i in range(100)], "ai_made": 7}
    cc.save_state(db, "fact", state, ist(THURSDAY, 14))
    stored = json.loads(db.get_content_state("fact")["recent"])
    assert stored["recent"] == [f"item {i}" for i in range(60, 100)] and stored["ai_made"] == 7


class _ctx:
    def __init__(self, stream):
        self._stream = stream

    def __enter__(self):
        return self._stream

    def __exit__(self, *exc):
        return False


def test_gemini_request_keeps_the_key_in_a_header_and_out_of_errors(monkeypatch):
    seen = {}

    def ok(request, timeout=0):
        seen["url"], seen["headers"], seen["body"] = request.full_url, {k.lower(): v for k, v in request.header_items()}, json.loads(request.data)
        reply = {"candidates": [{"content": {"parts": [{"text": json.dumps(GOOD["fact"])}]}}]}
        return _ctx(io.BytesIO(json.dumps(reply).encode()))
    monkeypatch.setattr(cc.urllib.request, "urlopen", ok)
    assert cc.ask_gemini("write a fact", KEY, model="gemini-test") == GOOD["fact"]
    assert seen["url"] == "https://generativelanguage.googleapis.com/v1beta/models/gemini-test:generateContent"
    assert KEY not in seen["url"] and seen["headers"]["x-goog-api-key"] == KEY
    assert seen["body"]["generationConfig"]["responseMimeType"] == "application/json"

    def denied(request, timeout=0):
        raise urllib.error.HTTPError(request.full_url, 429, f"quota for {KEY}", {}, io.BytesIO(KEY.encode()))
    monkeypatch.setattr(cc.urllib.request, "urlopen", denied)
    with pytest.raises(cc.ContentError) as caught:
        cc.ask_gemini("x", KEY, model="m")
    assert "HTTP 429" in str(caught.value) and KEY not in str(caught.value)

    for body in (b"<html>", b'{"candidates": []}', b'{"candidates": [{"content": {"parts": [{"text": "not json"}]}}]}',
                 b'{"candidates": [{"content": {"parts": [{"text": "[1, 2]"}]}}]}'):
        monkeypatch.setattr(cc.urllib.request, "urlopen", lambda request, timeout=0, body=body: _ctx(io.BytesIO(body)))
        with pytest.raises(cc.ContentError):
            cc.ask_gemini("x", KEY, model="m")


# -- quiz on Telegram ----------------------------------------------------------------------

def test_send_quiz_builds_a_quiz_poll(monkeypatch):
    client, calls = TelegramClient("123:TEST"), []
    monkeypatch.setattr(client, "_call", lambda method, payload=None: calls.append((method, payload)) or {"message_id": 55})
    assert client.send_quiz(CHAT, "What is XAU?", ["Gold", "Silver"], 0, "XAU is gold.") == "55"
    assert calls == [("sendPoll", {"chat_id": CHAT, "question": "What is XAU?", "options": [{"text": "Gold"}, {"text": "Silver"}],
                                   "type": "quiz", "correct_option_id": 0, "is_anonymous": True, "explanation": "XAU is gold."})]
    for bad in (("", ["a", "b"], 0, ""), ("q", ["only one"], 0, ""), ("q", ["a", "b"], 2, ""), ("q", ["a", "b"], True, ""),
                ("q" * 301, ["a", "b"], 0, ""), ("q", ["a", "x" * 101], 0, ""), ("q", ["a", "b"], 0, "e" * 201)):
        with pytest.raises(TelegramMessageError):
            client.send_quiz(CHAT, *bad)


# -- what's moving gold --------------------------------------------------------------------

class FakeFred:
    def __init__(self, observations=None, reason=None, error=None):
        self.observations, self.reason, self.error, self.requests = observations or {}, reason, error, []

    def unavailable_reason(self):
        return self.reason

    def fetch(self, requests):
        self.requests = requests
        if self.error:
            raise self.error
        return self.observations


FRED_DATA = {
    "DTWEXBGS": {date(2026, 10, 5): Decimal("121.3300"), date(2026, 10, 6): Decimal("121.4500")},
    "DGS10": {date(2026, 10, 6): Decimal("4.15"), date(2026, 10, 7): Decimal("4.12")},
    "DFEDTARU": {date(2026, 10, 7): Decimal("4.25"), date(2026, 10, 8): Decimal("4.25")},
}


def test_whats_moving_gold_text():
    readings = cards.read_drivers(settings(fred_api_key="k"), THURSDAY, provider=FakeFred(FRED_DATA))
    assert cards.build_drivers(THURSDAY, readings) == "\n".join([
        "🧭 *WHAT'S MOVING GOLD* · Thu, 8 Oct",
        "Latest official US figures",
        "",
        "💵 US dollar index (broad)\n   121.45  🔺 +0.12  (6 Oct)",
        "📈 US 10-year bond yield\n   4.12%  🔻 -0.03%  (7 Oct)",
        "🏦 Fed interest rate (upper limit)\n   4.25%  ▪️ unchanged  (8 Oct)",
        "",
        "_A stronger dollar and higher yields have usually weighed on gold, and the reverse has usually supported it._",
        "ℹ️ Published once a day by FRED, so a day or more old. Not trading advice.",
    ])


def test_whats_moving_gold_goes_out_at_its_time_and_waits_when_fred_cannot_answer(db):
    client, moment = FakeTelegram(), ist(THURSDAY, 11, 55)                 # the 11:00 pulse's grace is over
    fetch = lambda url, **_: pulse.PriceQuote(4100.0, moment.astimezone(timezone.utc), "s")
    provider = FakeFred(FRED_DATA)
    results = cards.send_scheduled(db, client, settings(fred_api_key="k"), CHAT, now=moment, fetch=fetch, drivers_provider=provider)
    assert [(r.kind, r.outcome) for r in results] == [(cards.DRIVERS, cards.OUTCOME_SENT)]
    assert "WHAT'S MOVING GOLD" in client.texts[0] and [r.series_id for r in provider.requests] == ["DTWEXBGS", "DGS10", "DFEDTARU"]
    for broken in (FakeFred(reason="FRED_API_KEY is not set."), FakeFred(error=ProviderError("FRED answered HTTP 500.")), FakeFred({})):
        with SQLiteRepository(":memory:") as other:
            again = cards.send_scheduled(other, FakeTelegram(), settings(fred_api_key="k"), CHAT, now=moment, fetch=fetch, drivers_provider=broken)
            assert [(r.kind, r.outcome) for r in again] == [(cards.DRIVERS, cards.OUTCOME_NOT_READY)]


# -- news countdown ------------------------------------------------------------------------

def event(name, utc, impact="High", currency="USD", forecast="0.3%", previous="0.2%"):
    local, stamp = utc.astimezone(IST), "2026-10-08T00:00:00Z"
    return Event(
        event_id="ff-" + re.sub(r"\W", "", name).lower()[:12], date=local.date().isoformat(), time=local.strftime("%H:%M"),
        timezone="Asia/Kolkata", datetime_utc=utc.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), currency=currency, event_name=name,
        impact=impact, original_impact=impact, gold_relevance=True, forecast=forecast, previous=previous, actual=None,
        source="forexfactory", source_url="https://example.test/feed", retrieved_at=stamp, updated_at=stamp)


def countdown_run(db, client, moment):
    fetch = lambda url, **_: pulse.PriceQuote(4100.0, moment.astimezone(timezone.utc), "s")
    return [r for r in cards.send_scheduled(db, client, settings(), CHAT, now=moment, fetch=fetch) if r.kind == cards.COUNTDOWN]


def test_countdown_text():
    release = ist(THURSDAY, 18, 0)
    text = cards.build_countdown(event("Core CPI m/m", release), release, ist(THURSDAY, 17, 32), IST)
    assert text == "\n".join([
        "⏰ *NEWS IN 28 MINUTES*",
        "",
        "*Core CPI m/m*",
        "USD · high impact · 6:00 PM IST",
        "",
        "Forecast: 0.3%   Previous: 0.2%",
        "",
        "⚠️ Prices can move fast and spreads can widen around the release.",
        "ℹ️ Information only, not trading advice.",
    ])
    bare = cards.build_countdown(event("FOMC Statement", release, forecast=None, previous=None), release, ist(THURSDAY, 17, 32), IST)
    assert "Forecast" not in bare and "Previous" not in bare


def test_one_countdown_about_half_an_hour_before_high_impact_news_only(db):
    db.upsert_events([
        event("Core CPI m/m", ist(THURSDAY, 18, 0)), event("Unemployment Claims", ist(THURSDAY, 18, 0), impact="Medium"),
        event("ECB Rate Decision", ist(THURSDAY, 18, 0), currency="EUR"), event("FOMC Statement", ist(THURSDAY, 23, 30)),
    ])
    client, sent_at = FakeTelegram(), []
    moment = ist(THURSDAY, 16, 2)
    while moment <= ist(THURSDAY, 18, 30):
        for result in countdown_run(db, client, moment):
            if result.outcome == cards.OUTCOME_SENT:
                sent_at.append(moment.strftime("%H:%M"))
        moment += timedelta(minutes=10)
    assert sent_at == ["17:32"]                                          # once, 28 minutes before, for the one high-impact USD event
    countdown = [t for t in client.texts if "NEWS IN" in t]
    assert len(countdown) == 1 and "Core CPI m/m" in countdown[0] and "Unemployment Claims" not in countdown[0]


def test_countdown_also_works_in_quiet_hours_because_news_does_not_wait(db):
    db.upsert_events([event("FOMC Meeting Minutes", ist(THURSDAY, 0, 30))])
    client = FakeTelegram()
    results = countdown_run(db, client, ist(THURSDAY, 0, 3))
    assert [r.outcome for r in results] == [cards.OUTCOME_SENT] and "FOMC Meeting Minutes" in client.texts[0]


# -- the whole day -------------------------------------------------------------------------

def test_a_full_weekday_posts_one_card_an_hour_in_the_planned_order(db):
    from tests.test_cards import record_day
    record_day(db, date(2026, 10, 7), [4000 + i for i in range(80)])
    client, config, provider = FakeTelegram(), settings(fred_api_key="k"), FakeFred(FRED_DATA)
    timeline, moment = [], ist(THURSDAY, 0, 4)
    while moment.date() == THURSDAY:
        def content(card_slot, moment=moment):
            return cc.send_content_card(db, client, config, CHAT, card_slot, LIBRARY, now=moment)
        fetch = lambda url, moment=moment, **_: pulse.PriceQuote(4100.0, moment.astimezone(timezone.utc), "s")
        for result in cards.send_scheduled(db, client, config, CHAT, now=moment, fetch=fetch, drivers_provider=provider,
                                           extra_cards={cards.CORNER: content, cards.LEARN: content}):
            if result.outcome == cards.OUTCOME_SENT:
                timeline.append((moment.strftime("%H:%M"), result.kind))
        moment += timedelta(minutes=10)
    assert timeline == [
        ("08:34", "KEY_LEVELS"), ("09:04", "MARKET_PULSE"), ("10:04", "TRADER_CORNER"), ("11:04", "MARKET_PULSE"),
        ("11:34", "GOLD_DRIVERS"), ("12:04", "TRADER_CORNER"), ("13:04", "MARKET_PULSE"), ("14:04", "TRADER_CORNER"),
        ("15:04", "MARKET_PULSE"), ("15:34", "LEARN_CARD"), ("16:04", "TRADER_CORNER"), ("17:04", "MARKET_PULSE"),
        ("18:04", "TRADER_CORNER"), ("19:04", "MARKET_PULSE"), ("20:04", "TRADER_CORNER"), ("21:04", "MARKET_PULSE"),
        ("22:04", "TRADER_CORNER"), ("23:04", "MARKET_PULSE"), ("23:34", "DAILY_RECAP"),
    ]
    assert len(client.quizzes) == 2 and len(client.texts) == 17
    # No two cards in a row look the same.
    kinds = [kind for _, kind in timeline]
    assert all(a != b for a, b in zip(kinds, kinds[1:]))


def test_the_weekend_keeps_the_channel_alive_without_market_cards(db):
    client, config, kinds = FakeTelegram(), settings(), []
    moment = ist(SATURDAY, 0, 4)
    while moment.date() == SATURDAY:
        def content(card_slot, moment=moment):
            return cc.send_content_card(db, client, config, CHAT, card_slot, LIBRARY, now=moment)
        fetch = lambda url, moment=moment, **_: pulse.PriceQuote(4100.0, moment.astimezone(timezone.utc), "s")
        for result in cards.send_scheduled(db, client, config, CHAT, now=moment, fetch=fetch,
                                           extra_cards={cards.CORNER: content, cards.LEARN: content}):
            if result.outcome == cards.OUTCOME_SENT:
                kinds.append(result.kind)
        moment += timedelta(minutes=10)
    assert kinds.count("TRADER_CORNER") == 7 and kinds.count("LEARN_CARD") == 1 and len(kinds) == 8
