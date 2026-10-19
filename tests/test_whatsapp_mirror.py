"""WhatsApp copies of the low-volume Telegram posts."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.database.database import SQLiteRepository
from src.delivery import mirror
from src.delivery.models import DeliveryError

NOW = datetime(2026, 10, 19, 10, 0, tzinfo=timezone.utc)
SETTINGS = SimpleNamespace(whatsapp_announcement_chat_id="120363000000005555@newsletter", whapi_token="tok-TEST")


class FakeWhapi:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send_text(self, chat_id, text):
        if self.fail:
            raise DeliveryError("Whapi answered HTTP 503.")
        self.sent.append((chat_id, text))
        return f"wamid-{len(self.sent)}"


@pytest.fixture
def db():
    with SQLiteRepository(":memory:") as repo:
        yield repo


def test_off_unless_switched_on():
    for value in (None, "", "false", "no"):
        env = {} if value is None else {"WHATSAPP_MIRROR": value}
        assert mirror.whatsapp_mirror(SETTINGS, environ=env, make_client=lambda s: FakeWhapi()) is None


def test_without_a_destination_copies_stay_off():
    settings = SimpleNamespace(whatsapp_announcement_chat_id=None, whapi_token="tok-TEST")
    assert mirror.whatsapp_mirror(settings, environ={"WHATSAPP_MIRROR": "true"}, make_client=lambda s: FakeWhapi()) is None


def test_only_the_low_volume_posts_are_copied_and_each_once(db):
    fake = FakeWhapi()
    copy = mirror.whatsapp_mirror(SETTINGS, environ={"WHATSAPP_MIRROR": "true"}, make_client=lambda s: fake)
    for kind in mirror.MIRRORED_TYPES:
        assert copy(db, f"{kind}_1", kind, f"text of {kind}", NOW).outcome == "SENT"
        assert copy(db, f"{kind}_1", kind, f"text of {kind}", NOW).outcome == "ALREADY_SENT"
    for kind in ("MARKET_PULSE", "TRADER_CORNER", "LEARN_CARD", "KEY_LEVELS", "DAILY_RECAP", "GOLD_DRIVERS"):
        assert copy(db, f"{kind}_1", kind, "hourly card", NOW) is None
    assert [t for _, t in fake.sent] == [f"text of {k}" for k in mirror.MIRRORED_TYPES]
    assert {c for c, _ in fake.sent} == {"120363000000005555@newsletter"}
    record = db.get_delivery("VIDEO_POST_1", "whapi", "120363000000005555@newsletter")
    assert record.destination == "whatsapp_channel" and record.status == "SENT"


def test_a_failed_copy_is_recorded_and_reported(db):
    copy = mirror.whatsapp_mirror(SETTINGS, environ={"WHATSAPP_MIRROR": "true"}, make_client=lambda s: FakeWhapi(fail=True))
    result = copy(db, "LIVE_ALERT_1", "LIVE_ALERT", "We are live", NOW)
    assert result.outcome == "FAILED"
    assert mirror.describe(result).startswith("WhatsApp copy FAILED")
    assert mirror.describe(None) is None
