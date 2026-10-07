"""Delivers Step 4 messages to WhatsApp and records the outcome.

Delivery only: the message text comes from the content engine and is passed
on byte for byte. This module never builds, edits or re-words a message.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..config import Settings
from ..content.models import Message
from ..database.base import EventRepository
from .models import (
    DESTINATION_WHATSAPP_ANNOUNCEMENT, FAILED, OUTCOME_ALREADY_SENT, OUTCOME_DRY_RUN, OUTCOME_FAILED, OUTCOME_SENT,
    PROVIDER_WHAPI, SENT, DeliveryRecord, DeliveryResult,
)
from .whapi import WhapiClient, WhapiConfigError, WhapiError, mask_chat_id

log = logging.getLogger(__name__)

TEST_MESSAGE_TYPE = "WHATSAPP_TEST"
TEST_MESSAGE_TEXT = (
    "🧪 WHATSAPP AUTOMATION TEST\n"
    "\n"
    "✅ WhatsApp connection is working.\n"
    "\n"
    "This is a test message from the USD + Gold Market News automation system."
)


def build_whapi_client(settings: Settings) -> WhapiClient:
    return WhapiClient(settings.whapi_token, base_url=settings.whapi_base_url,
                       timeout=settings.request_timeout_seconds, user_agent=settings.user_agent)


def announcement_chat_id(settings: Settings) -> str:
    chat_id = (settings.whatsapp_announcement_chat_id or "").strip()
    if not chat_id:
        raise WhapiConfigError("WHATSAPP_ANNOUNCEMENT_CHAT_ID is not set. Add it to the environment or to .env.")
    return chat_id


def _stamp(moment: datetime | None = None) -> str:
    return (moment or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def deliver_text(
    db: EventRepository,
    client: WhapiClient | None,
    *,
    message_key: str,
    message_type: str,
    text: str,
    chat_id: str,
    dry_run: bool = False,
    now: datetime | None = None,
) -> DeliveryResult:
    """Send one text to the WhatsApp announcement group, at most once per message_key.

    A message that was already sent successfully is skipped. A dry run sends
    nothing and stores nothing. A failure is recorded so it can be retried.
    """
    existing = db.get_delivery(message_key, PROVIDER_WHAPI, chat_id)
    if existing is not None and existing.status == SENT:
        log.info("Delivery skipped, already sent: %s -> %s", message_key, mask_chat_id(chat_id))
        return DeliveryResult(OUTCOME_ALREADY_SENT, message_key, existing.provider_message_id, sent_at=existing.sent_at)
    if dry_run:
        return DeliveryResult(OUTCOME_DRY_RUN, message_key)

    stamp = _stamp(now)
    record = DeliveryRecord(
        message_key=message_key, provider=PROVIDER_WHAPI, destination_id=chat_id,
        destination=DESTINATION_WHATSAPP_ANNOUNCEMENT, message_type=message_type, status=FAILED,
        attempts=(existing.attempts + 1) if existing else 1,
        created_at=existing.created_at if existing else stamp, updated_at=stamp,
    )
    try:
        if client is None:
            raise WhapiConfigError("No WhatsApp client is configured.")
        record.provider_message_id = client.send_text(chat_id, text)
        record.status, record.sent_at = SENT, stamp
    except WhapiError as exc:
        record.error = str(exc)
        db.save_delivery(record)
        log.error("Delivery failed: %s -> %s: %s", message_key, mask_chat_id(chat_id), exc)
        return DeliveryResult(OUTCOME_FAILED, message_key, error=str(exc))
    db.save_delivery(record)
    log.info("Delivered: %s -> %s (provider id %s)", message_key, mask_chat_id(chat_id), record.provider_message_id)
    return DeliveryResult(OUTCOME_SENT, message_key, record.provider_message_id, sent_at=stamp)


def deliver_message(db: EventRepository, client: WhapiClient | None, message: Message, chat_id: str, *,
                    dry_run: bool = False, now: datetime | None = None) -> DeliveryResult:
    """Deliver a Step 4 message. Its text is passed on exactly as the content engine produced it."""
    return deliver_text(db, client, message_key=message.message_key, message_type=message.message_type,
                        text=message.text, chat_id=chat_id, dry_run=dry_run, now=now)


def send_test_message(db: EventRepository, client: WhapiClient, chat_id: str, now: datetime | None = None) -> DeliveryResult:
    """Send the fixed connection-test text once. Each call is its own message."""
    return deliver_text(db, client, message_key=f"WHATSAPP_TEST_{_stamp(now)}", message_type=TEST_MESSAGE_TYPE,
                        text=TEST_MESSAGE_TEXT, chat_id=chat_id, now=now)
