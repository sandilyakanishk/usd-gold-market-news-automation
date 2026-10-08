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
    DESTINATION_TELEGRAM_CHANNEL, DESTINATION_WHATSAPP_ANNOUNCEMENT, FAILED, OUTCOME_ALREADY_SENT, OUTCOME_DRY_RUN,
    OUTCOME_FAILED, OUTCOME_SENT, PROVIDER_TELEGRAM, PROVIDER_WHAPI, SENT, DeliveryError, DeliveryRecord, DeliveryResult,
)
from .telegram import TelegramClient, TelegramConfigError, describe_chat
from .whapi import WhapiClient, WhapiConfigError, mask_chat_id

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
    provider: str = PROVIDER_WHAPI,
    destination: str = DESTINATION_WHATSAPP_ANNOUNCEMENT,
    send_options: dict | None = None,
    label: str | None = None,
) -> DeliveryResult:
    """Send one text to one destination, at most once per message_key and provider.

    A message that was already sent successfully is skipped. A dry run sends
    nothing and stores nothing. A failure is recorded so it can be retried.
    Each provider keeps its own record, so WhatsApp and Telegram are independent.
    """
    label = label or mask_chat_id(chat_id)
    existing = db.get_delivery(message_key, provider, chat_id)
    if existing is not None and existing.status == SENT:
        log.info("Delivery skipped, already sent: %s -> %s", message_key, label)
        return DeliveryResult(OUTCOME_ALREADY_SENT, message_key, existing.provider_message_id, sent_at=existing.sent_at)
    if dry_run:
        return DeliveryResult(OUTCOME_DRY_RUN, message_key)

    stamp = _stamp(now)
    record = DeliveryRecord(
        message_key=message_key, provider=provider, destination_id=chat_id,
        destination=destination, message_type=message_type, status=FAILED,
        attempts=(existing.attempts + 1) if existing else 1,
        created_at=existing.created_at if existing else stamp, updated_at=stamp,
    )
    try:
        if client is None:
            raise WhapiConfigError("No WhatsApp client is configured.")
        record.provider_message_id = client.send_text(chat_id, text, **(send_options or {}))
        record.status, record.sent_at = SENT, stamp
    except DeliveryError as exc:
        record.error = str(exc)
        db.save_delivery(record)
        log.error("Delivery failed: %s -> %s: %s", message_key, label, exc)
        return DeliveryResult(OUTCOME_FAILED, message_key, error=str(exc))
    db.save_delivery(record)
    log.info("Delivered: %s -> %s via %s (provider id %s)", message_key, label, provider, record.provider_message_id)
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


# -- Telegram -------------------------------------------------------------------------------

TELEGRAM_TEST_MESSAGE_TYPE = "TELEGRAM_TEST"
TELEGRAM_TEST_MESSAGE_TEXT = (
    "🧪 TELEGRAM AUTOMATION TEST\n"
    "\n"
    "✅ Telegram connection is working.\n"
    "\n"
    "This is a test message from the USD + Gold Market News automation system."
)


def build_telegram_client(settings: Settings) -> TelegramClient:
    return TelegramClient(settings.telegram_bot_token, timeout=settings.request_timeout_seconds)


def telegram_chat_id(settings: Settings) -> str:
    chat_id = (settings.telegram_chat_id or "").strip()
    if not chat_id:
        raise TelegramConfigError("TELEGRAM_CHAT_ID is not set. Add it to the environment or to .env.")
    return chat_id


def deliver_telegram_message(db: EventRepository, client: TelegramClient | None, message: Message, chat_id: str, *,
                             dry_run: bool = False, now: datetime | None = None) -> DeliveryResult:
    """Deliver a Step 4 message to Telegram. The text is the one WhatsApp receives, byte for byte."""
    return deliver_text(
        db, client, message_key=message.message_key, message_type=message.message_type, text=message.text,
        chat_id=chat_id, dry_run=dry_run, now=now, provider=PROVIDER_TELEGRAM, destination=DESTINATION_TELEGRAM_CHANNEL,
        send_options={"markdown": message.markdown_safe}, label=describe_chat(chat_id))


def send_telegram_test_message(db: EventRepository, client: TelegramClient, chat_id: str,
                               now: datetime | None = None) -> DeliveryResult:
    return deliver_text(
        db, client, message_key=f"TELEGRAM_TEST_{_stamp(now)}", message_type=TELEGRAM_TEST_MESSAGE_TYPE,
        text=TELEGRAM_TEST_MESSAGE_TEXT, chat_id=chat_id, now=now, provider=PROVIDER_TELEGRAM,
        destination=DESTINATION_TELEGRAM_CHANNEL, send_options={"markdown": False}, label=describe_chat(chat_id))
