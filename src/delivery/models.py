"""Delivery record: proof that a generated message was (or was not) handed to a provider."""

from __future__ import annotations

from dataclasses import asdict, dataclass

SENT, FAILED = "SENT", "FAILED"
DELIVERY_STATUSES = (SENT, FAILED)

PROVIDER_WHAPI = "whapi"
DESTINATION_WHATSAPP_ANNOUNCEMENT = "whatsapp_community_announcement"
PROVIDER_TELEGRAM = "telegram"
DESTINATION_TELEGRAM_CHANNEL = "telegram_channel"


class DeliveryError(Exception):
    """A provider could not accept a message. Messages never contain credentials."""

# Outcomes of one delivery attempt (not all of them are stored).
OUTCOME_SENT = "SENT"
OUTCOME_ALREADY_SENT = "ALREADY_SENT"
OUTCOME_DRY_RUN = "DRY_RUN"
OUTCOME_FAILED = "FAILED"


@dataclass
class DeliveryRecord:
    message_key: str  # the Step 4 message identity
    provider: str  # e.g. "whapi"
    destination_id: str  # the provider's address for the destination, e.g. a WhatsApp chat id
    destination: str  # what kind of destination it is, e.g. "whatsapp_community_announcement"
    message_type: str
    status: str  # SENT / FAILED
    provider_message_id: str | None = None
    sent_at: str | None = None
    error: str | None = None
    attempts: int = 1
    created_at: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DeliveryResult:
    outcome: str  # SENT / ALREADY_SENT / DRY_RUN / FAILED
    message_key: str
    provider_message_id: str | None = None
    error: str | None = None
    sent_at: str | None = None
