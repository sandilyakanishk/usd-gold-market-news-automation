"""Telegram delivery with the network replaced. Nothing here contacts Telegram or posts anywhere."""

import io
import json
import urllib.error
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from src import main as cli
from src.actuals import service as actuals_service
from src.collector import forex_factory
from src.content.builder import build_content_builder
from src.content.fixtures import load_preview_fixture
from src.database.database import SQLiteRepository
from src.delivery import telegram
from src.delivery.models import DeliveryError, DeliveryRecord
from src.delivery.service import (
    TELEGRAM_TEST_MESSAGE_TEXT, deliver_message, deliver_telegram_message, send_telegram_test_message,
)
from src.delivery.telegram import (
    TelegramAuthError, TelegramClient, TelegramConfigError, TelegramDestinationError, TelegramError,
    TelegramMessageError, TelegramNetworkError, describe_chat,
)
from src.delivery.whapi import WhapiError

from .test_actuals_service import FixtureProvider
from .test_delivery_service import CHAT as WHATSAPP_CHAT, FakeWhapi
from .test_repository_contract import repo  # noqa: F401  (fixture)
from .test_retrieval import fake_urlopen

TOKEN = "1234567890:AAH-test_only_SECRET_token_0123456789ab"
CHANNEL = "@ExampleNewsChannel"
PRIVATE = "-1001234567890"
T1 = datetime(2026, 11, 12, 3, 30, tzinfo=timezone.utc)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def patch(monkeypatch, replies, calls=None):
    """`replies` maps a Bot API method to a result, a full reply dict, an exception, or a list of those (in order)."""
    def urlopen(request, timeout=None):
        method = request.full_url.rsplit("/", 1)[1]
        if calls is not None:
            calls.append((method, json.loads(request.data.decode("utf-8")), request))
        reply = replies[method]
        if isinstance(reply, list):
            reply = reply.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        if not (isinstance(reply, dict) and "ok" in reply):
            reply = {"ok": True, "result": reply}
        return FakeResponse(json.dumps(reply).encode("utf-8"))
    monkeypatch.setattr(telegram.urllib.request, "urlopen", urlopen)


def api_error(code, description):
    body = json.dumps({"ok": False, "error_code": code, "description": description}).encode("utf-8")
    return urllib.error.HTTPError(f"https://api.telegram.org/bot{TOKEN}/x", code, "err", None, io.BytesIO(body))


ME = {"id": 42, "is_bot": True, "username": "example_news_bot"}
ADMIN = {"status": "administrator", "can_post_messages": True}


# -- client ---------------------------------------------------------------------------

def test_send_text_posts_the_exact_text_and_returns_the_message_id(monkeypatch):
    calls = []
    patch(monkeypatch, {"sendMessage": {"message_id": 77, "chat": {"id": -100}}}, calls)
    text = "━━━━━━━━━━━━━━━━━━\n🇺🇸 *USD + GOLD MORNING BRIEF*\n1️⃣ *CPI m/m*\nForecast: 0.3%\n_note_"
    assert TelegramClient(TOKEN, timeout=9).send_text(CHANNEL, text) == "77"
    (method, payload, request), = calls
    assert method == "sendMessage"
    assert payload == {"chat_id": CHANNEL, "text": text, "disable_web_page_preview": True, "parse_mode": "Markdown"}
    assert request.get_method() == "POST" and request.get_header("Content-type") == "application/json"
    assert "python-urllib" not in request.get_header("User-agent").lower()


def test_plain_mode_sends_the_same_text_without_a_parse_mode(monkeypatch):
    calls = []
    patch(monkeypatch, {"sendMessage": {"message_id": 5}}, calls)
    TelegramClient(TOKEN).send_text(CHANNEL, "a_b *c", markdown=False)
    assert calls[0][1] == {"chat_id": CHANNEL, "text": "a_b *c", "disable_web_page_preview": True}


def test_unreadable_formatting_falls_back_to_plain_text_once(monkeypatch):
    calls = []
    patch(monkeypatch, {"sendMessage": [api_error(400, "Bad Request: can't parse entities: Can't find end of the entity"),
                                        {"message_id": 9}]}, calls)
    assert TelegramClient(TOKEN).send_text(CHANNEL, "odd _text") == "9"
    assert [("parse_mode" in payload, payload["text"]) for _, payload, _ in calls] == [(True, "odd _text"), (False, "odd _text")]


def test_other_message_errors_are_not_retried(monkeypatch):
    calls = []
    patch(monkeypatch, {"sendMessage": api_error(400, "Bad Request: message is too long")}, calls)
    with pytest.raises(TelegramMessageError):
        TelegramClient(TOKEN).send_text(CHANNEL, "x")
    assert len(calls) == 1


def test_check_destination(monkeypatch):
    calls = []
    patch(monkeypatch, {"getMe": ME, "getChat": {"id": -100, "type": "channel", "title": "Example"}, "getChatMember": ADMIN}, calls)
    assert TelegramClient(TOKEN).check_destination(CHANNEL) == {
        "bot": "example_news_bot", "title": "Example", "type": "channel", "status": "administrator"}
    assert [c[0] for c in calls] == ["getMe", "getChat", "getChatMember"]
    assert calls[2][1] == {"chat_id": CHANNEL, "user_id": 42}
    assert not any(c[0] == "sendMessage" for c in calls)


@pytest.mark.parametrize("member", [
    {"status": "administrator", "can_post_messages": False}, {"status": "left"}, {"status": "kicked"}, {"status": "member"},
])
def test_bot_without_posting_rights_is_reported(monkeypatch, member):
    patch(monkeypatch, {"getMe": ME, "getChat": {"id": -100, "type": "channel"}, "getChatMember": member})
    with pytest.raises(TelegramDestinationError, match="may not post there"):
        TelegramClient(TOKEN).check_destination(CHANNEL)


@pytest.mark.parametrize("token", [None, "", "   "])
def test_missing_token(token):
    with pytest.raises(TelegramConfigError, match="TELEGRAM_BOT_TOKEN is not set"):
        TelegramClient(token)


@pytest.mark.parametrize("chat", [None, "", "  "])
def test_missing_chat(monkeypatch, chat):
    calls = []
    patch(monkeypatch, {"sendMessage": {"message_id": 1}}, calls)
    with pytest.raises(TelegramConfigError, match="TELEGRAM_CHAT_ID is not set"):
        TelegramClient(TOKEN).send_text(chat, "hello")
    with pytest.raises(TelegramConfigError, match="TELEGRAM_CHAT_ID is not set"):
        TelegramClient(TOKEN).check_destination(chat)
    assert calls == []


@pytest.mark.parametrize("text, message", [("", "no text"), ("  \n", "no text"), (None, "no text"), ("x" * 4097, "limit is 4096")])
def test_unsendable_text_is_rejected_before_any_request(monkeypatch, text, message):
    calls = []
    patch(monkeypatch, {"sendMessage": {"message_id": 1}}, calls)
    with pytest.raises(TelegramMessageError, match=message):
        TelegramClient(TOKEN).send_text(CHANNEL, text)
    assert calls == []


@pytest.mark.parametrize("error, expected, text", [
    (api_error(401, "Unauthorized"), TelegramAuthError, "Check TELEGRAM_BOT_TOKEN"),
    (api_error(404, "Not Found"), TelegramAuthError, "Check TELEGRAM_BOT_TOKEN"),
    (api_error(400, "Bad Request: chat not found"), TelegramDestinationError, "Check TELEGRAM_CHAT_ID"),
    (api_error(403, "Forbidden: bot is not a member of the channel chat"), TelegramDestinationError, "administrator"),
    (api_error(400, "Bad Request: not enough rights to send text messages to the chat"), TelegramDestinationError, "administrator"),
    (api_error(429, "Too Many Requests: retry after 5"), TelegramError, "Too Many Requests"),
    (api_error(500, "Internal Server Error"), TelegramError, "500"),
    (urllib.error.URLError("getaddrinfo failed"), TelegramNetworkError, "Could not reach Telegram"),
    (TimeoutError("timed out"), TelegramNetworkError, "Could not reach Telegram"),
])
def test_failures_are_classified_and_never_leak_the_token(monkeypatch, error, expected, text):
    patch(monkeypatch, {"sendMessage": error})
    with pytest.raises(expected) as raised:
        TelegramClient(TOKEN).send_text(CHANNEL, "hello")
    message = str(raised.value)
    assert text in message and TOKEN not in message and "api.telegram.org" not in message
    assert isinstance(raised.value, DeliveryError)


@pytest.mark.parametrize("reply", [{"ok": True, "result": {}}, {"ok": True, "result": True}, {"ok": False, "description": "nope"}])
def test_unconfirmed_send_is_an_error(monkeypatch, reply):
    patch(monkeypatch, {"sendMessage": reply})
    with pytest.raises(TelegramError):
        TelegramClient(TOKEN).send_text(CHANNEL, "hello")


def test_chat_references_for_display():
    assert describe_chat(CHANNEL) == "@ExampleNewsChannel"       # a public name is not a secret
    assert describe_chat(PRIVATE) == "...7890" and "100123456" not in describe_chat(PRIVATE)
    assert describe_chat(None) == "(not set)"


def test_both_providers_share_one_error_base():
    assert issubclass(TelegramError, DeliveryError) and issubclass(WhapiError, DeliveryError)


# -- service ----------------------------------------------------------------------------------

class FakeTelegram:
    def __init__(self, error=None):
        self.sent, self.error = [], error

    def send_text(self, chat_id, text, *, markdown=True):
        if self.error:
            raise self.error
        self.sent.append((chat_id, text, markdown))
        return str(100 + len(self.sent))


@pytest.fixture
def messages(settings):
    items, now = load_preview_fixture(settings)
    builder = build_content_builder(settings, now)
    return {"morning": builder.morning_update(items, date(2026, 11, 12)), "alert": builder.high_alerts(items)[0],
            "actual": next(m for m in builder.actual_results(items) if m.attribution),
            "upcoming": builder.upcoming_reminders(items)[0]}


@pytest.mark.parametrize("kind", ["morning", "alert", "actual", "upcoming"])
def test_each_message_type_reaches_telegram_exactly_as_generated(repo, messages, kind):  # noqa: F811
    message, client = messages[kind], FakeTelegram()
    result = deliver_telegram_message(repo, client, message, CHANNEL, now=T1)
    assert (result.outcome, result.provider_message_id) == ("SENT", "101")
    assert client.sent == [(CHANNEL, message.text, True)]
    assert repo.get_delivery(message.message_key, "telegram", CHANNEL) == DeliveryRecord(
        message_key=message.message_key, provider="telegram", destination_id=CHANNEL, destination="telegram_channel",
        message_type=message.message_type, status="SENT", provider_message_id="101", sent_at="2026-11-12T03:30:00Z",
        error=None, attempts=1, created_at="2026-11-12T03:30:00Z", updated_at="2026-11-12T03:30:00Z")


def test_telegram_and_whatsapp_receive_the_same_text_and_are_tracked_separately(repo, messages):  # noqa: F811
    message, tg, wa = messages["actual"], FakeTelegram(), FakeWhapi()
    assert deliver_message(repo, wa, message, WHATSAPP_CHAT, now=T1).outcome == "SENT"
    assert deliver_telegram_message(repo, tg, message, CHANNEL, now=T1).outcome == "SENT"
    assert wa.sent[0][1] == tg.sent[0][1] == message.text
    assert "not endorsed or certified by the Federal Reserve Bank of St. Louis" in tg.sent[0][1]   # attribution kept
    # Each destination has its own record; sending to one never blocks or repeats the other.
    assert deliver_telegram_message(repo, tg, message, CHANNEL).outcome == "ALREADY_SENT"
    assert deliver_message(repo, wa, message, WHATSAPP_CHAT).outcome == "ALREADY_SENT"
    assert (len(tg.sent), len(wa.sent), repo.count_deliveries()) == (1, 1, 2)
    assert {r.provider for r in repo.list_deliveries()} == {"telegram", "whapi"}


def test_telegram_dry_run_failure_and_retry(repo, messages):  # noqa: F811
    message = messages["alert"]
    assert deliver_telegram_message(repo, None, message, CHANNEL, dry_run=True).outcome == "DRY_RUN"
    assert repo.count_deliveries() == 0
    failing = FakeTelegram(error=TelegramNetworkError("Could not reach Telegram: timed out"))
    result = deliver_telegram_message(repo, failing, message, CHANNEL, now=T1)
    assert (result.outcome, result.error) == ("FAILED", "Could not reach Telegram: timed out")
    assert repo.get_delivery(message.message_key, "telegram", CHANNEL).status == "FAILED"
    working = FakeTelegram()
    assert deliver_telegram_message(repo, working, message, CHANNEL, now=T1).outcome == "SENT"
    record = repo.get_delivery(message.message_key, "telegram", CHANNEL)
    assert (record.status, record.attempts, record.error) == ("SENT", 2, None)


def test_a_message_with_unsafe_markers_is_sent_as_plain_text(repo, messages):  # noqa: F811
    odd = replace(messages["alert"], markdown_safe=False)
    client = FakeTelegram()
    deliver_telegram_message(repo, client, odd, CHANNEL, now=T1)
    assert client.sent == [(CHANNEL, odd.text, False)]


def test_telegram_test_message(repo):  # noqa: F811
    client = FakeTelegram()
    result = send_telegram_test_message(repo, client, CHANNEL, now=T1)
    assert (result.outcome, result.message_key) == ("SENT", "TELEGRAM_TEST_2026-11-12T03:30:00Z")
    assert client.sent == [(CHANNEL, TELEGRAM_TEST_MESSAGE_TEXT, False)]
    assert repo.get_delivery(result.message_key, "telegram", CHANNEL).message_type == "TELEGRAM_TEST"


# -- command line -----------------------------------------------------------------------------

class FakeTelegramClient(FakeTelegram):
    check_error = None

    def check_destination(self, chat_id):
        if self.check_error:
            raise self.check_error
        return {"bot": "example_news_bot", "title": "Example", "type": "channel", "status": "administrator"}


@pytest.fixture
def run(monkeypatch, settings, feed_text, capsys):
    base = replace(settings, telegram_bot_token=TOKEN, telegram_chat_id=CHANNEL, whapi_token="wa-token",
                   whatsapp_announcement_chat_id=WHATSAPP_CHAT)
    state = {"settings": base, "now": (2026, 10, 8, 9, 0), "tg_built": 0}
    tg, wa = FakeTelegramClient(), FakeWhapi()
    sources = {"BLS": FixtureProvider("BLS"), "FRED": FixtureProvider("FRED", {"ICSA": {date(2026, 10, 3): Decimal("218000")}})}
    monkeypatch.setattr(cli.Settings, "from_env", classmethod(lambda cls: state["settings"]))
    monkeypatch.setattr(forex_factory.urllib.request, "urlopen", fake_urlopen(feed_text))
    monkeypatch.setattr(actuals_service, "build_providers", lambda settings: sources)
    monkeypatch.setattr(cli, "build_whapi_client", lambda settings: wa)

    def build(settings):
        state["tg_built"] += 1
        return tg
    monkeypatch.setattr(cli, "build_telegram_client", build)

    class Clock(cli.datetime):
        @classmethod
        def now(cls, tz=None):
            from zoneinfo import ZoneInfo
            return cls(*state["now"], tzinfo=ZoneInfo("Asia/Kolkata")).astimezone(tz)

    monkeypatch.setattr(cli, "datetime", Clock)
    monkeypatch.setattr(actuals_service, "datetime", Clock)

    def _run(*argv, now=None, **overrides):
        if now:
            state["now"] = now
        state["settings"] = replace(base, **overrides)
        return cli.main(list(argv)), capsys.readouterr()
    _run.tg, _run.wa, _run.state = tg, wa, state
    _run("--enrich-actuals", "--week")          # calendar synced, classified, actuals checked
    return _run


def deliveries(settings):
    with SQLiteRepository(settings.database_path) as db:
        return db.list_deliveries()


def test_telegram_check_command(run, settings):
    code, out = run("--telegram-check")
    assert code == 0
    assert out.out == ("Telegram bot: OK (@example_news_bot)\n"
                       "Channel: @ExampleNewsChannel (channel), bot is administrator and may post\nNothing was sent.\n")
    assert run.tg.sent == [] and deliveries(settings) == []
    run.tg.check_error = TelegramDestinationError("The bot is 'left' in @ExampleNewsChannel and may not post there.")
    code, out = run("--telegram-check")
    assert code == 1 and out.err.startswith("TELEGRAM ERROR: The bot is 'left'")


def test_telegram_dry_run_shows_the_step_4_text_and_sends_nothing(run, settings):
    _, preview = run("--preview-morning")
    code, out = run("--telegram-send-morning", "--dry-run")
    assert code == 0
    assert out.out.startswith("DRY RUN: nothing is sent and no delivery is recorded.\n\n"
                              "===== DAILY_UPDATE_2026-10-08 | MORNING_UPDATE =====\n"
                              "Destination: telegram_channel (@ExampleNewsChannel) via telegram\nWould send:\n")
    assert preview.out.split("=====\n", 1)[1].rstrip("\n") in out.out
    assert run.tg.sent == [] and deliveries(settings) == [] and run.state["tg_built"] == 0
    assert TOKEN not in out.out + out.err


def test_telegram_send_once_then_skip(run, settings):
    _, preview = run("--preview-morning", "--json")
    (message,) = json.loads(preview.out)
    code, out = run("--telegram-send-morning")
    assert code == 0 and "Sent. Telegram message ID: 101" in out.out
    assert run.tg.sent == [(CHANNEL, message["text"], True)]
    (record,) = deliveries(settings)
    assert (record.provider, record.destination, record.status) == ("telegram", "telegram_channel", "SENT")
    code, out = run("--telegram-send-morning")
    assert code == 0 and "Already sent — skipped. (sent" in out.out and "Telegram message ID 101" in out.out
    assert len(run.tg.sent) == 1


def test_both_channels_in_one_command_get_the_same_message(run, settings):
    code, out = run("--whatsapp-send-morning", "--telegram-send-morning")
    assert code == 0
    assert out.out.count("===== DAILY_UPDATE_2026-10-08 | MORNING_UPDATE =====") == 2
    assert "via whapi" in out.out and "via telegram" in out.out
    assert run.wa.sent[0][1] == run.tg.sent[0][1]
    assert sorted(r.provider for r in deliveries(settings)) == ["telegram", "whapi"]


def test_one_channel_failing_does_not_stop_the_other(run, settings):
    run.tg.error = TelegramNetworkError("Could not reach Telegram: timed out")
    code, out = run("--whatsapp-send-morning", "--telegram-send-morning")
    assert code == 1
    assert "Sent. Whapi message ID: wamid-1" in out.out and "FAILED: Could not reach Telegram: timed out" in out.out
    assert len(run.wa.sent) == 1
    # A missing Telegram setting is reported but WhatsApp still goes out.
    code, out = run("--whatsapp-send-actuals", "--telegram-send-actuals", telegram_chat_id=None)
    assert code == 1 and "TELEGRAM ERROR: TELEGRAM_CHAT_ID is not set" in out.err and "No eligible ACTUAL_RESULT message." in out.out


def test_results_alerts_and_reminders_on_telegram(run, settings):
    code, out = run("--telegram-send-alert", "--telegram-send-actuals", "--telegram-send-upcoming")
    assert code == 0 and out.out.count("No eligible") == 3 and run.tg.sent == []
    run("--enrich-actuals", "--week", now=(2026, 10, 8, 18, 30))                     # claims released
    code, out = run("--telegram-send-actuals")
    assert code == 0 and len(run.tg.sent) == 1
    text = run.tg.sent[0][1]
    for part in ("🚨 *USD DATA RELEASED*", "🇺🇸 *Unemployment Claims*", "Actual: *218K*", "*Result:* 📈 ABOVE FORECAST",
                 "Source: FRED (calendar: Forex Factory)", "not endorsed or certified"):
        assert part in text


def test_telegram_test_command_and_missing_settings(run, settings, monkeypatch):
    code, out = run("--telegram-test")
    assert code == 0 and "Test message sent to @ExampleNewsChannel." in out.out and "Telegram message ID: 101" in out.out
    assert run.tg.sent == [(CHANNEL, TELEGRAM_TEST_MESSAGE_TEXT, False)]
    code, out = run("--telegram-send-morning", telegram_chat_id=None)
    assert code == 1 and "TELEGRAM ERROR: TELEGRAM_CHAT_ID is not set" in out.err
    from src.delivery import service as delivery_service
    monkeypatch.setattr(cli, "build_telegram_client", delivery_service.build_telegram_client)
    for argv in (["--telegram-check"], ["--telegram-test"], ["--telegram-send-morning"]):
        code, out = run(*argv, telegram_bot_token=None)
        assert code == 1 and "TELEGRAM ERROR: TELEGRAM_BOT_TOKEN is not set" in out.err, argv


def test_sample_events_cannot_be_posted_to_telegram_for_real(run):
    code, out = run("--telegram-send-alert", "--fixture")
    assert code == 2 and "only be used with --dry-run" in out.err and run.tg.sent == []
    code, out = run("--telegram-send-alert", "--fixture", "--dry-run")
    assert code == 0 and "🚨 *HIGH-IMPACT USD ALERT*" in out.out and run.tg.sent == []


def test_token_never_appears_in_output_or_logs(run, caplog):
    with caplog.at_level("DEBUG"):
        outputs = [run(*argv)[1] for argv in (["--telegram-check"], ["--telegram-test"], ["--telegram-send-morning", "--dry-run"],
                                              ["--telegram-send-morning"], ["--telegram-send-morning"])]
    assert TOKEN not in "".join(o.out + o.err for o in outputs) + caplog.text
