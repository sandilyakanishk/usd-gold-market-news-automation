"""WhatsApp copies of the Telegram posts.

After a scheduled card (market pulse, trader's corner, learn card, key
levels, what's moving gold, recap, week ahead), a news countdown, a new reel
or video, or a live alert has gone out on Telegram, the same text is sent
once to the WhatsApp destination as well. A quiz goes as text, with the
answer underneath, because a WhatsApp Channel has no quiz poll. The news
messages themselves (brief, alerts, results, reminders) have their own
WhatsApp steps.

Whapi's free plan allows about 1,000 requests a month. A month of these
copies is roughly 650 of them, which is why the WhatsApp destination check
runs only in the morning (see runplan).

If WhatsApp fails, the post stays in the whatsapp_outbox table and later
runs try again (catch_up), so a short WhatsApp outage does not lose posts.

Switched on by WHATSAPP_MIRROR=true (in the cloud, the WHATSAPP_ENABLED
repository variable). Each copy has its own delivery record (provider whapi),
so it is sent at most once.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta

from ..config import Settings
from ..database.base import EventRepository
from .models import OUTCOME_SENT, PROVIDER_WHAPI, SENT, DeliveryError, DeliveryResult
from .service import announcement_chat_id, build_whapi_client, deliver_text
from .whapi import is_channel

log = logging.getLogger(__name__)

STAMP = "%Y-%m-%dT%H:%M:%SZ"
# A copy that failed is tried again on later runs for this long, and at most this many times in all.
CATCH_UP_HOURS = 12
MAX_ATTEMPTS = 5
MIRRORED_TYPES = ("MARKET_PULSE", "TRADER_CORNER", "LEARN_CARD", "KEY_LEVELS", "GOLD_DRIVERS", "DAILY_RECAP",
                  "WEEK_AHEAD", "NEWS_COUNTDOWN", "VIDEO_POST", "LIVE_ALERT")


def _download_cover(url: str) -> bytes | None:
    from ..social import download_image
    return download_image(url)


def whatsapp_mirror(settings: Settings, *, environ=None, make_client=build_whapi_client, download=_download_cover):
    """A function (db, message_key, message_type, text, now) -> DeliveryResult | None, or None when switched off."""
    env = os.environ if environ is None else environ
    if str(env.get("WHATSAPP_MIRROR", "")).strip().lower() != "true":
        return None
    try:
        chat_id = announcement_chat_id(settings)
        client = make_client(settings)
    except DeliveryError as exc:
        log.warning("WhatsApp copies are off: %s", exc)
        return None
    destination = "whatsapp_channel" if is_channel(chat_id) else "whatsapp_community_announcement"

    def copy(db: EventRepository, message_key: str, message_type: str, text: str, now: datetime,
             images: list[str] | None = None) -> DeliveryResult | None:
        if message_type not in MIRRORED_TYPES:
            return None
        # Kept first, so that if WhatsApp fails now, a later run sends it (see catch_up).
        db.add_to_outbox(message_key, message_type, text, json.dumps(images or []), now.strftime(STAMP))
        return _send(db, message_key, message_type, text, images or [], now)

    def _send(db, message_key, message_type, text, images, now) -> DeliveryResult:
        return deliver_text(db, _WhatsAppPost(client, images, download), message_key=message_key,
                            message_type=message_type, text=text, chat_id=chat_id, now=now, provider=PROVIDER_WHAPI,
                            destination=destination)

    def catch_up(db: EventRepository, now: datetime) -> list[tuple[str, DeliveryResult]]:
        """Send what WhatsApp has not got yet from the last CATCH_UP_HOURS. Gives up on a post after MAX_ATTEMPTS."""
        results = []
        for row in db.outbox_since((now - timedelta(hours=CATCH_UP_HOURS)).strftime(STAMP)):
            record = db.get_delivery(row["message_key"], PROVIDER_WHAPI, chat_id)
            if record is not None and (record.status == SENT or record.attempts >= MAX_ATTEMPTS):
                continue
            try:
                images = json.loads(row["images"] or "[]")
            except ValueError:
                images = []
            results.append((row["message_key"], _send(db, row["message_key"], row["message_type"], row["text"], images, now)))
        return results

    copy.catch_up = catch_up
    return copy


class _WhatsAppPost:
    """Sends a copy with its cover image when one can be had, otherwise as plain text without a link preview."""

    def __init__(self, client, images: list[str], download):
        self._client, self._images, self._download = client, images, download

    def send_text(self, chat_id: str, text: str, **_: object) -> str:
        for url in self._images:
            image = self._download(url)
            if image is None:
                continue
            try:
                return self._client.send_image(chat_id, image, text)
            except DeliveryError as exc:
                log.warning("WhatsApp did not take the cover image (%s); sending the text alone.", exc)
                break
        return self._client.send_text(chat_id, text, link_preview=False)


def describe(result: DeliveryResult | None) -> str | None:
    """One line for the run log, or None when nothing was copied."""
    if result is None:
        return None
    if result.outcome == OUTCOME_SENT:
        return f"WhatsApp copy sent. Whapi message ID: {result.provider_message_id}"
    if result.outcome == "ALREADY_SENT":
        return "WhatsApp copy: already sent."
    return f"WhatsApp copy FAILED: {result.error}"
