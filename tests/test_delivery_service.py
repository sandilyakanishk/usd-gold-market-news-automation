"""Delivery of Step 4 messages with a stand-in WhatsApp client, on every backend
(SQLite always; PostgreSQL when TEST_DATABASE_URL is set)."""

import copy
from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from src.content.builder import build_content_builder
from src.content.fixtures import load_preview_fixture
from src.database.base import DatabaseError
from src.delivery.models import DeliveryRecord
from src.delivery.service import TEST_MESSAGE_TEXT, deliver_message, send_test_message
from src.delivery.whapi import WhapiAuthError, WhapiNetworkError

from .test_repository_contract import repo  # noqa: F401  (fixture)

CHAT = "120363000000008282@g.us"
OTHER_CHAT = "120363000000003507@g.us"
T1 = datetime(2026, 11, 12, 3, 30, tzinfo=timezone.utc)
T2 = datetime(2026, 11, 12, 4, 0, tzinfo=timezone.utc)


class FakeWhapi:
    """Records what it is asked to send. Stands in for WhapiClient."""

    def __init__(self, error=None):
        self.sent, self.error = [], error

    def send_text(self, chat_id, text):
        if self.error:
            raise self.error
        self.sent.append((chat_id, text))
        return f"wamid-{len(self.sent)}"


@pytest.fixture
def messages(settings):
    """Real Step 4 messages built from the sample events: one of each type."""
    items, now = load_preview_fixture(settings)
    builder = build_content_builder(settings, now)
    return {
        "morning": builder.morning_update(items, date(2026, 11, 12)),
        "alert": builder.high_alerts(items)[0],
        "actual": next(m for m in builder.actual_results(items) if m.attribution),   # FRED-sourced
        "upcoming": builder.upcoming_reminders(items)[0],
    }


@pytest.mark.parametrize("kind, message_type, key_prefix", [
    ("morning", "MORNING_UPDATE", "DAILY_UPDATE_2026-11-12"),
    ("alert", "HIGH_ALERT", "HIGH_ALERT_ff-"),
    ("actual", "ACTUAL_RESULT", "ACTUAL_ff-"),
    ("upcoming", "UPCOMING_REMINDER", "UPCOMING_ff-"),
])
def test_each_message_type_is_delivered_exactly_as_generated(repo, messages, kind, message_type, key_prefix):  # noqa: F811
    message, client = messages[kind], FakeWhapi()
    original = copy.deepcopy(message)
    result = deliver_message(repo, client, message, CHAT, now=T1)

    assert (result.outcome, result.provider_message_id, result.error) == ("SENT", "wamid-1", None)
    assert client.sent == [(CHAT, original.text)]          # the adapter added, removed and changed nothing
    assert message == original                              # and left the message object alone
    assert message.message_key.startswith(key_prefix)

    record = repo.get_delivery(message.message_key, "whapi", CHAT)
    assert record == DeliveryRecord(
        message_key=message.message_key, provider="whapi", destination_id=CHAT,
        destination="whatsapp_community_announcement", message_type=message_type, status="SENT",
        provider_message_id="wamid-1", sent_at="2026-11-12T03:30:00Z", error=None, attempts=1,
        created_at="2026-11-12T03:30:00Z", updated_at="2026-11-12T03:30:00Z")


def test_step_4_content_survives_delivery_intact(repo, messages):  # noqa: F811
    client = FakeWhapi()
    deliver_message(repo, client, messages["actual"], CHAT, now=T1)
    (_, text), = client.sent
    for part in ("📰 US core PCE inflation comes in above expectations", "🇺🇸 *Core PCE Price Index m/m*",
                 "Previous: 0.2%", "Forecast: 0.2%", "Actual: *0.3%*", "Result: 🔺 ABOVE FORECAST",
                 "Priority: 🔴 CRITICAL", "Source: FRED (calendar: Forex Factory)",
                 "_This product uses the FRED® API but is not endorsed or certified by the Federal Reserve Bank of St. Louis._"):
        assert part in text
    assert text == messages["actual"].text


def test_a_message_is_never_sent_twice(repo, messages):  # noqa: F811
    client = FakeWhapi()
    assert deliver_message(repo, client, messages["alert"], CHAT, now=T1).outcome == "SENT"
    for _ in range(3):
        again = deliver_message(repo, client, messages["alert"], CHAT, now=T2)
        assert (again.outcome, again.provider_message_id, again.sent_at) == ("ALREADY_SENT", "wamid-1", "2026-11-12T03:30:00Z")
    assert len(client.sent) == 1 and repo.count_deliveries() == 1
    record = repo.get_delivery(messages["alert"].message_key, "whapi", CHAT)
    assert (record.attempts, record.updated_at) == (1, "2026-11-12T03:30:00Z")


def test_different_messages_and_destinations_are_independent(repo, messages):  # noqa: F811
    client = FakeWhapi()
    for kind in ("morning", "alert", "actual", "upcoming"):
        assert deliver_message(repo, client, messages[kind], CHAT, now=T1).outcome == "SENT"
    assert deliver_message(repo, client, messages["alert"], OTHER_CHAT, now=T1).outcome == "SENT"  # another group
    assert len(client.sent) == 5 and repo.count_deliveries() == 5
    assert [r.message_key for r in repo.list_deliveries()] == sorted(r.message_key for r in repo.list_deliveries())


def test_a_revised_actual_is_a_new_message(repo, messages):  # noqa: F811
    client, first = FakeWhapi(), messages["actual"]
    revised = replace(first, message_key=first.message_key[:-1] + "2", text=first.text.replace("0.3%", "0.4%"))
    assert deliver_message(repo, client, first, CHAT, now=T1).outcome == "SENT"
    assert deliver_message(repo, client, revised, CHAT, now=T2).outcome == "SENT"
    assert deliver_message(repo, client, first, CHAT, now=T2).outcome == "ALREADY_SENT"
    assert len(client.sent) == 2


def test_dry_run_sends_nothing_and_records_nothing(repo, messages):  # noqa: F811
    client = FakeWhapi()
    result = deliver_message(repo, client, messages["morning"], CHAT, dry_run=True, now=T1)
    assert (result.outcome, result.provider_message_id) == ("DRY_RUN", None)
    assert client.sent == [] and repo.count_deliveries() == 0
    assert deliver_message(repo, None, messages["morning"], CHAT, dry_run=True).outcome == "DRY_RUN"  # needs no client
    # A dry run does not block the real send, and reports an earlier real send truthfully.
    assert deliver_message(repo, client, messages["morning"], CHAT, now=T1).outcome == "SENT"
    assert deliver_message(repo, client, messages["morning"], CHAT, dry_run=True).outcome == "ALREADY_SENT"


@pytest.mark.parametrize("error", [
    WhapiAuthError("Whapi answered HTTP 401. Check WHAPI_TOKEN."),
    WhapiNetworkError("Could not reach Whapi: timed out"),
])
def test_failure_is_recorded_and_can_be_retried(repo, messages, error):  # noqa: F811
    failing = FakeWhapi(error=error)
    result = deliver_message(repo, failing, messages["alert"], CHAT, now=T1)
    assert (result.outcome, result.error, result.provider_message_id) == ("FAILED", str(error), None)
    record = repo.get_delivery(messages["alert"].message_key, "whapi", CHAT)
    assert (record.status, record.error, record.sent_at, record.provider_message_id, record.attempts) == (
        "FAILED", str(error), None, None, 1)

    assert deliver_message(repo, failing, messages["alert"], CHAT, now=T2).outcome == "FAILED"
    assert repo.get_delivery(messages["alert"].message_key, "whapi", CHAT).attempts == 2

    working = FakeWhapi()
    assert deliver_message(repo, working, messages["alert"], CHAT, now=T2).outcome == "SENT"
    record = repo.get_delivery(messages["alert"].message_key, "whapi", CHAT)
    assert (record.status, record.error, record.attempts, record.provider_message_id) == ("SENT", None, 3, "wamid-1")
    assert (record.created_at, record.sent_at) == ("2026-11-12T03:30:00Z", "2026-11-12T04:00:00Z")
    assert repo.count_deliveries() == 1


def test_missing_client_is_a_recorded_failure_not_a_crash(repo, messages):  # noqa: F811
    result = deliver_message(repo, None, messages["alert"], CHAT, now=T1)
    assert result.outcome == "FAILED" and "No WhatsApp client" in result.error


def test_a_sent_record_cannot_be_overwritten(repo, messages):  # noqa: F811
    key = messages["alert"].message_key
    deliver_message(repo, FakeWhapi(), messages["alert"], CHAT, now=T1)
    sent = repo.get_delivery(key, "whapi", CHAT)
    repo.save_delivery(replace(sent, status="FAILED", provider_message_id=None, sent_at=None, error="late failure",
                               attempts=9, updated_at="2026-11-13T00:00:00Z"))
    assert repo.get_delivery(key, "whapi", CHAT) == sent


def test_uniqueness_is_enforced_by_the_database(repo, messages):  # noqa: F811
    from src.database.base import DELIVERY_COLUMNS, DELIVERIES_TABLE
    deliver_message(repo, FakeWhapi(), messages["alert"], CHAT, now=T1)
    values = repo._encode_delivery(repo.get_delivery(messages["alert"].message_key, "whapi", CHAT))
    insert = f"INSERT INTO {DELIVERIES_TABLE} ({', '.join(DELIVERY_COLUMNS)}) VALUES ({', '.join('?' for _ in DELIVERY_COLUMNS)})"
    with pytest.raises(DatabaseError):
        with repo._transaction():
            repo._write(insert, [values[c] for c in DELIVERY_COLUMNS])
    assert repo.count_deliveries() == 1


def test_delivery_does_not_touch_events_classifications_or_actuals(repo, messages, parse, feed_text, settings):  # noqa: F811
    from src.classification.service import classify_events
    repo.upsert_events(e for e in parse(feed_text).events if e.currency == "USD")
    classify_events(settings, repo, now="2026-10-08T00:00:00Z")
    ids = [e.event_id for e in repo.query_events()]
    before = (repo.query_events(), repo.get_classifications(ids), repo.get_actual_records(ids))
    for kind in messages:
        deliver_message(repo, FakeWhapi(), messages[kind], CHAT, now=T1)
    assert (repo.query_events(), repo.get_classifications(ids), repo.get_actual_records(ids)) == before
    assert repo.count_deliveries() == 4


def test_test_message(repo):  # noqa: F811
    client = FakeWhapi()
    result = send_test_message(repo, client, CHAT, now=T1)
    assert (result.outcome, result.message_key, result.provider_message_id) == ("SENT", "WHATSAPP_TEST_2026-11-12T03:30:00Z", "wamid-1")
    assert client.sent == [(CHAT, TEST_MESSAGE_TEXT)]
    assert TEST_MESSAGE_TEXT == ("🧪 WHATSAPP AUTOMATION TEST\n\n✅ WhatsApp connection is working.\n\n"
                                 "This is a test message from the USD + Gold Market News automation system.")
    record = repo.get_delivery(result.message_key, "whapi", CHAT)
    assert (record.message_type, record.status, record.destination) == ("WHATSAPP_TEST", "SENT", "whatsapp_community_announcement")
    # A later test is its own message; the same instant is not repeated.
    assert send_test_message(repo, client, CHAT, now=T2).outcome == "SENT"
    assert send_test_message(repo, client, CHAT, now=T2).outcome == "ALREADY_SENT"
    assert len(client.sent) == 2
