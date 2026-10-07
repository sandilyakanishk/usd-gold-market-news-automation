"""WhatsApp commands with the Whapi client replaced. Nothing here sends a real message."""

import json
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from src import main as cli
from src.actuals import service
from src.collector import forex_factory
from src.database.database import SQLiteRepository
from src.delivery.whapi import WhapiAuthError, WhapiNetworkError, WhapiSessionError

from .test_actuals_service import FixtureProvider
from .test_delivery_service import CHAT, FakeWhapi
from .test_retrieval import fake_urlopen

COMMUNITY = "120363000000001940@g.us"
TOKEN = "tok-SECRET-abcdef0123456789"


class FakeClient(FakeWhapi):
    def __init__(self):
        super().__init__()
        self.health_error, self.announcement = None, CHAT

    def health(self):
        if self.health_error:
            raise self.health_error
        return {"status": "AUTH"}

    def announcement_group_id(self, community_id):
        return self.announcement


@pytest.fixture
def client():
    return FakeClient()


@pytest.fixture
def run(monkeypatch, settings, feed_text, capsys, client):
    """CLI on the fixture feed week, 8 Oct 2026 09:00 India time, with WhatsApp configured."""
    base = replace(settings, whapi_token=TOKEN, whatsapp_community_id=COMMUNITY, whatsapp_announcement_chat_id=CHAT)
    state = {"settings": base, "now": (2026, 10, 8, 9, 0), "built": 0}
    sources = {"BLS": FixtureProvider("BLS"), "FRED": FixtureProvider("FRED", {"ICSA": {date(2026, 10, 3): Decimal("218000")}})}
    monkeypatch.setattr(cli.Settings, "from_env", classmethod(lambda cls: state["settings"]))
    monkeypatch.setattr(forex_factory.urllib.request, "urlopen", fake_urlopen(feed_text))
    monkeypatch.setattr(service, "build_providers", lambda settings: sources)

    def build(settings):
        state["built"] += 1
        return client
    monkeypatch.setattr(cli, "build_whapi_client", build)

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
        state["settings"] = replace(base, **overrides)
        code = cli.main(list(argv))
        return code, capsys.readouterr()
    _run.state = state
    return _run


@pytest.fixture
def prepared(run):
    run("--enrich-actuals", "--week")   # calendar synced, classified, actuals checked
    return run


def deliveries(settings):
    with SQLiteRepository(settings.database_path) as db:
        return db.list_deliveries()


# -- check and test message --------------------------------------------------------

def test_check_reports_and_sends_nothing(run, client, settings):
    code, out = run("--whatsapp-check")
    assert code == 0
    assert out.out == ("Whapi connection: OK (WhatsApp session authenticated)\n"
                       "Announcement group: ...8282@g.us\n"
                       "Community ...1940@g.us: announcement group confirmed\n"
                       "Nothing was sent.\n")
    assert client.sent == [] and not settings.database_path.exists()


def test_check_detects_a_wrong_announcement_group(run, client):
    client.announcement = "120363000000003507@g.us"
    code, out = run("--whatsapp-check")
    assert code == 1 and "DESTINATION MISMATCH" in out.err and client.sent == []


def test_check_without_community_id(run):
    code, out = run("--whatsapp-check", whatsapp_community_id=None)
    assert code == 0 and "could not be cross-checked" in out.out


def test_check_reports_a_disconnected_session(run, client):
    client.health_error = WhapiSessionError("The WhatsApp session is not connected (status: QR).")
    code, out = run("--whatsapp-check")
    assert code == 1 and out.err.startswith("WHATSAPP ERROR: The WhatsApp session is not connected")


def test_test_message_is_sent_once_and_recorded(run, client, settings):
    code, out = run("--whatsapp-test")
    assert code == 0
    assert client.sent == [(CHAT, "🧪 WHATSAPP AUTOMATION TEST\n\n✅ WhatsApp connection is working.\n\n"
                                  "This is a test message from the USD + Gold Market News automation system.")]
    assert "Test message sent to ...8282@g.us." in out.out and "Whapi message ID: wamid-1" in out.out
    (record,) = deliveries(settings)
    assert (record.message_type, record.status, record.provider, record.destination, record.provider_message_id) == (
        "WHATSAPP_TEST", "SENT", "whapi", "whatsapp_community_announcement", "wamid-1")


def test_test_message_failure_is_reported(run, client):
    client.error = WhapiAuthError("Whapi answered HTTP 401. Check WHAPI_TOKEN.")
    code, out = run("--whatsapp-test")
    assert code == 1 and "WHATSAPP ERROR: test message not sent: Whapi answered HTTP 401" in out.err


# -- dry run -------------------------------------------------------------------------

def test_morning_dry_run_shows_the_real_message_and_sends_nothing(prepared, client, settings):
    _, preview = prepared("--preview-morning")
    code, out = prepared("--whatsapp-send-morning", "--dry-run")
    assert code == 0
    assert out.out.startswith("DRY RUN: nothing is sent and no delivery is recorded.\n\n"
                              "===== DAILY_UPDATE_2026-10-08 | MORNING_UPDATE =====\n"
                              "Destination: whatsapp_community_announcement (...8282@g.us) via whapi\n"
                              "Would send:\n📅 *USD + GOLD DAILY UPDATE*")
    step4_text = preview.out.split("=====\n", 1)[1].rstrip("\n")
    assert step4_text in out.out                       # exactly the Step 4 text
    assert client.sent == [] and deliveries(settings) == []
    assert prepared.state["built"] == 0                 # a dry run does not even create a client
    assert CHAT not in out.out and COMMUNITY not in out.out


def test_dry_run_needs_no_token(prepared, client):
    code, out = prepared("--whatsapp-send-morning", "--dry-run", whapi_token=None)
    assert code == 0 and "Would send:" in out.out and client.sent == []


# -- real sends (to the stand-in client) ------------------------------------------------

def test_morning_send_delivers_the_step_4_text_once(prepared, client, settings):
    _, preview = prepared("--preview-morning", "--json")
    (message,) = json.loads(preview.out)
    code, out = prepared("--whatsapp-send-morning")
    assert code == 0
    assert client.sent == [(CHAT, message["text"])]
    assert "===== DAILY_UPDATE_2026-10-08 | MORNING_UPDATE =====" in out.out and "Sent. Whapi message ID: wamid-1" in out.out
    (record,) = deliveries(settings)
    assert (record.message_key, record.status, record.message_type) == ("DAILY_UPDATE_2026-10-08", "SENT", "MORNING_UPDATE")

    code, out = prepared("--whatsapp-send-morning")
    assert code == 0 and "Already sent — skipped." in out.out
    assert len(client.sent) == 1 and len(deliveries(settings)) == 1
    _, out = prepared("--whatsapp-send-morning", "--dry-run")
    assert "Already sent — skipped." in out.out and "Would send" not in out.out


def test_morning_for_another_day_is_a_different_message(prepared, client):
    prepared("--whatsapp-send-morning")
    code, out = prepared("--whatsapp-send-morning", "--tomorrow")
    assert code == 0 and "DAILY_UPDATE_2026-10-09" in out.out and len(client.sent) == 2
    assert "*Prelim UoM Consumer Sentiment*" in client.sent[1][1]


def test_actuals_send_after_release(prepared, client, settings):
    code, out = prepared("--whatsapp-send-actuals")
    assert code == 0 and out.out == "No eligible ACTUAL_RESULT message.\n\n" and client.sent == []

    prepared("--enrich-actuals", "--week", now=(2026, 10, 8, 18, 30))     # claims released
    _, preview = prepared("--preview-actuals", "--json")
    (message,) = json.loads(preview.out)
    code, out = prepared("--whatsapp-send-actuals")
    assert code == 0 and client.sent == [(CHAT, message["text"])]
    text = client.sent[0][1]
    for part in ("📰 US weekly jobless claims come in above expectations", "🇺🇸 *Unemployment Claims*", "Previous: 197K",
                 "Forecast: 200K", "Actual: *218K*", "Source: FRED (calendar: Forex Factory)", "not endorsed or certified"):
        assert part in text
    assert deliveries(settings)[0].message_key == message["message_key"]
    assert "Already sent — skipped." in prepared("--whatsapp-send-actuals")[1].out and len(client.sent) == 1


def test_alert_and_upcoming_with_no_eligible_event(prepared, client, settings):
    code, out = prepared("--whatsapp-send-alert", "--whatsapp-send-upcoming")
    assert code == 0
    assert out.out == "No eligible HIGH_ALERT message.\n\nNo eligible UPCOMING_REMINDER message.\n\n"
    assert client.sent == [] and deliveries(settings) == []


def test_alert_and_upcoming_send_when_events_qualify(prepared, client, tmp_path, settings):
    config = json.loads(settings.message_templates_path.read_text(encoding="utf-8"))
    config["selection"].update(upcoming_minimum_priority="MEDIUM", alert_only_before_release=False)
    custom = tmp_path / "templates.json"
    custom.write_text(json.dumps(config), encoding="utf-8")
    code, out = prepared("--whatsapp-send-alert", "--whatsapp-send-upcoming", message_templates_path=custom)
    assert code == 0
    assert out.out.count("Sent. Whapi message ID:") == 5
    sent_texts = [text for _, text in client.sent]
    assert sent_texts[0].startswith("🚨 *HIGH IMPACT ALERT*") and "*FOMC Meeting Minutes*" in sent_texts[0]
    assert all(text.startswith("⏰ *UPCOMING USD EVENT*") for text in sent_texts[1:])
    keys = sorted(r.message_key for r in deliveries(settings))
    assert sum(k.startswith("HIGH_ALERT_") for k in keys) == 1 and sum(k.startswith("UPCOMING_") for k in keys) == 4
    again = prepared("--whatsapp-send-alert", "--whatsapp-send-upcoming", message_templates_path=custom)[1].out
    assert again.count("Already sent — skipped.") == 5 and len(client.sent) == 5


def test_empty_day_is_not_posted(prepared, client):
    code, out = prepared("--whatsapp-send-morning", "--date", "2026-10-04")
    assert code == 0 and out.out == "No eligible MORNING_UPDATE message.\n\n" and client.sent == []


def test_send_failure_is_reported_recorded_and_retryable(prepared, client, settings):
    client.error = WhapiNetworkError("Could not reach Whapi: timed out")
    code, out = prepared("--whatsapp-send-morning")
    assert code == 1 and "FAILED: Could not reach Whapi: timed out" in out.out
    (record,) = deliveries(settings)
    assert (record.status, record.error, record.attempts) == ("FAILED", "Could not reach Whapi: timed out", 1)
    client.error = None
    code, out = prepared("--whatsapp-send-morning")
    assert code == 0 and "Sent. Whapi message ID: wamid-1" in out.out
    assert deliveries(settings)[0].status == "SENT" and deliveries(settings)[0].attempts == 2


# -- configuration and safety --------------------------------------------------------

def test_missing_destination_is_a_clear_error(prepared, client):
    code, out = prepared("--whatsapp-send-morning", whatsapp_announcement_chat_id=None)
    assert code == 1 and out.err.startswith("WHATSAPP ERROR: WHATSAPP_ANNOUNCEMENT_CHAT_ID is not set")
    code, out = prepared("--whatsapp-test", whatsapp_announcement_chat_id=None)
    assert code == 1 and "WHATSAPP_ANNOUNCEMENT_CHAT_ID is not set" in out.err
    assert client.sent == []


def test_missing_token_is_a_clear_error(run, monkeypatch):
    from src.delivery import service as delivery_service
    monkeypatch.setattr(cli, "build_whapi_client", delivery_service.build_whapi_client)   # the real factory
    for argv in (["--whatsapp-check"], ["--whatsapp-test"], ["--whatsapp-send-morning"]):
        code, out = run(*argv, whapi_token=None)
        assert code == 1 and out.err.startswith("WHATSAPP ERROR: WHAPI_TOKEN is not set"), argv


def test_sample_events_cannot_be_sent_for_real(prepared, client):
    code, out = prepared("--whatsapp-send-alert", "--fixture")
    assert code == 2 and "only be used with --dry-run" in out.err and client.sent == []
    code, out = prepared("--whatsapp-send-alert", "--fixture", "--dry-run")
    assert code == 0 and "Would send:\n🚨 *HIGH IMPACT ALERT*" in out.out and client.sent == []


def test_secrets_and_full_ids_never_appear_in_output(prepared, client, caplog):
    with caplog.at_level("DEBUG"):
        outputs = [prepared(*argv)[1] for argv in (
            ["--whatsapp-check"], ["--whatsapp-test"], ["--whatsapp-send-morning", "--dry-run"], ["--whatsapp-send-morning"],
            ["--whatsapp-send-morning"])]
    text = "".join(o.out + o.err for o in outputs) + caplog.text
    assert TOKEN not in text and CHAT not in text and COMMUNITY not in text
    assert "...8282@g.us" in text


def test_sending_changes_no_event_classification_or_actual(prepared, client, settings):
    def snapshot():
        with SQLiteRepository(settings.database_path) as db:
            ids = [e.event_id for e in db.query_events()]
            return db.query_events(), db.get_classifications(ids), db.get_actual_records(ids)
    before = snapshot()
    views = (["--week", "--no-fetch"], ["--classify", "--week", "--no-fetch", "--json"], ["--actuals", "--no-fetch"],
             ["--preview-morning"])
    outputs = [prepared(*v)[1].out for v in views]
    prepared("--whatsapp-test")
    prepared("--whatsapp-send-morning")
    prepared("--whatsapp-send-alert", "--whatsapp-send-actuals", "--whatsapp-send-upcoming")
    assert snapshot() == before
    after = [prepared(*v)[1].out for v in views]
    assert after[0] == outputs[0] and after[2] == outputs[2] and after[3] == outputs[3]
    strip = lambda text: [{k: v for k, v in row["classification"].items() if k != "classified_at"} for row in json.loads(text)]
    assert strip(after[1]) == strip(outputs[1])


def test_the_delivery_layer_generates_no_content():
    """It may import the Message type and the fixed test text lives in it; it has no template or headline logic."""
    import ast
    import pathlib
    import src.delivery as delivery
    imported = set()
    for path in pathlib.Path(delivery.__path__[0]).glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.level:
                imported.add((node.module or "", tuple(sorted(a.name for a in node.names))))
    content_imports = [names for module, names in imported if module.startswith("content")]
    assert content_imports == [("Message",)]
