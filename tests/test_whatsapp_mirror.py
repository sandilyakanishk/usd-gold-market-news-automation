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


def test_every_telegram_card_is_copied_once_and_news_is_left_to_its_own_steps(db):
    fake = FakeWhapi()
    copy = mirror.whatsapp_mirror(SETTINGS, environ={"WHATSAPP_MIRROR": "true"}, make_client=lambda s: fake)
    for kind in mirror.MIRRORED_TYPES:
        assert copy(db, f"{kind}_1", kind, f"text of {kind}", NOW).outcome == "SENT"
        assert copy(db, f"{kind}_1", kind, f"text of {kind}", NOW).outcome == "ALREADY_SENT"
    for kind in ("MORNING_UPDATE", "HIGH_ALERT", "ACTUAL_RESULT", "UPCOMING_REMINDER", "TELEGRAM_TEST"):
        assert copy(db, f"{kind}_1", kind, "has its own WhatsApp step", NOW) is None
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


def test_a_month_of_copies_fits_the_free_allowance():
    """Weekday: 8 pulses, 7 corner cards, key levels, drivers, learn card, recap. Weekend: 7 corner, learn, week ahead."""
    from datetime import date, timedelta
    from zoneinfo import ZoneInfo
    from src import cards
    ist, start = ZoneInfo("Asia/Kolkata"), date(2026, 10, 1)
    copies = sum(len(cards.slots_for(start + timedelta(days=d), ist)) for d in range(31))
    news, videos_and_live, checks = 3 * 31, 2 * 31, 3 * 2 * 31
    assert copies < 520 and copies + news + videos_and_live + checks < 950


def test_a_quiz_goes_to_whatsapp_as_text_with_the_answer_underneath():
    from src import content_cards as cc
    item = {"question": "What does XAU stand for?", "options": ["Silver", "Gold", "Platinum", "Copper"], "answer": 1,
            "explanation": "XAU is the code for gold."}
    text = cc.quiz_as_text(item)
    options, correct = cc.arrange_quiz(item)
    assert text.startswith("❓ *QUIZ TIME*\n\nWhat does XAU stand for?\n\nA) ")
    assert f"✅ *Answer: {'ABCD'[correct]}) Gold*" in text and text.endswith("XAU is the code for gold.")
    assert text.index("Answer") > text.index("D) ")                  # the answer comes after all the options
