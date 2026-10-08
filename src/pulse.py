"""Market pulse: a short half-hourly post with the gold price and the next high-impact USD event.

    python -m src.main --telegram-send-pulse [--dry-run]

The post states numbers only: the current XAU/USD price, how it moved since
the previous post, and how long until the next high-impact USD release (or, in a week
with none left, the next medium-impact one). It is
built from a fixed template, never from a language model, and it carries no
trading view.

One pulse belongs to one half-hour slot. The slot is the message identity, so
however often the automation runs, each slot is posted at most once. Nothing
is posted while the gold market is closed for the weekend or when the price
source has stopped updating.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from .config import Settings
from .database.base import EventRepository
from .delivery.models import (
    DESTINATION_TELEGRAM_CHANNEL, OUTCOME_ALREADY_SENT, OUTCOME_DRY_RUN, OUTCOME_FAILED, OUTCOME_SENT,
    PROVIDER_TELEGRAM, SENT,
)
from .delivery.service import deliver_text
from .delivery.telegram import TelegramClient, describe_chat

log = logging.getLogger(__name__)

SYMBOL = "XAU"
MESSAGE_TYPE = "MARKET_PULSE"
SLOT_MINUTES = 30
# A quote older than this means the source has stopped updating (holiday, outage): no post.
MAX_QUOTE_AGE = timedelta(minutes=45)
# The change line compares with the previous post only if that post is recent enough to mean something.
MAX_COMPARE_AGE = timedelta(hours=24)
KEEP_SNAPSHOTS = timedelta(days=14)

OUTCOME_MARKET_CLOSED = "MARKET_CLOSED"
OUTCOME_STALE_PRICE = "STALE_PRICE"

DISCLAIMER = "ℹ️ Indicative price. Information only, not trading advice."


class PriceError(Exception):
    """The gold price could not be obtained."""


@dataclass(frozen=True)
class PriceQuote:
    price: float
    updated_at: datetime  # when the source last updated the price (UTC)
    source: str


@dataclass(frozen=True)
class PulseResult:
    outcome: str
    message_key: str | None = None
    text: str | None = None
    provider_message_id: str | None = None
    sent_at: str | None = None
    error: str | None = None


# -- price source ---------------------------------------------------------------------------

def parse_quote(body: str, source: str) -> PriceQuote:
    """Read the price source's JSON reply: {"price": 4114.39, "updatedAt": "2026-10-08T10:34:37Z", ...}."""
    try:
        data = json.loads(body)
        price = float(data["price"])
        updated = datetime.fromisoformat(str(data["updatedAt"]).replace("Z", "+00:00"))
    except (ValueError, TypeError, KeyError) as exc:
        raise PriceError(f"The gold price source returned an unexpected reply ({type(exc).__name__}).") from exc
    if not (price > 0) or price != price or price == float("inf"):
        raise PriceError("The gold price source returned a price that is not a positive number.")
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return PriceQuote(price, updated.astimezone(timezone.utc), source)


def fetch_gold_price(url: str, *, timeout: int = 20, user_agent: str = "") -> PriceQuote:
    """One GET to the price source. No key is needed."""
    request = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "application/json"})
    host = urlparse(url).netloc or "the price source"
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise PriceError(f"The gold price source ({host}) answered HTTP {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PriceError(f"The gold price source ({host}) could not be reached: {type(exc).__name__}.") from exc
    return parse_quote(body, host)


# -- timing ---------------------------------------------------------------------------------

def market_is_open(now: datetime) -> bool:
    """Spot gold trades from Sunday 22:00 UTC to Friday 21:00 UTC."""
    utc = now.astimezone(timezone.utc)
    weekday, hour = utc.weekday(), utc.hour  # Monday is 0
    if weekday == 5:
        return False
    if weekday == 4 and hour >= 21:
        return False
    if weekday == 6 and hour < 22:
        return False
    return True


def slot_start(now: datetime) -> datetime:
    """The start of the half-hour slot `now` falls in (UTC)."""
    utc = now.astimezone(timezone.utc)
    return utc.replace(minute=utc.minute - utc.minute % SLOT_MINUTES, second=0, microsecond=0)


def slot_key(slot: datetime) -> str:
    return slot.strftime("%Y-%m-%dT%H:%M:%SZ")


def message_key(slot: datetime) -> str:
    return f"{MESSAGE_TYPE}_{slot.strftime('%Y-%m-%dT%H%MZ')}"


# -- text -----------------------------------------------------------------------------------

def _clock(moment: datetime) -> str:
    hour = moment.hour % 12 or 12
    return f"{hour}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"


def _day(moment: datetime) -> str:
    return f"{moment.strftime('%a')}, {moment.day} {moment.strftime('%b')}"


def format_countdown(delta: timedelta) -> str:
    minutes = max(1, int(delta.total_seconds() // 60))
    days, rest = divmod(minutes, 24 * 60)
    hours, mins = divmod(rest, 60)
    if days:
        return f"in {days}d {hours}h"
    if hours:
        return f"in {hours}h {mins}m"
    return f"in {mins}m"


def format_change(price: float, previous: float, since: datetime) -> str:
    diff = round(price - previous, 2)
    if diff == 0:
        return f"▪️ Unchanged since {_clock(since)}"
    percent = diff / previous * 100
    sign, arrow = ("+", "🔺") if diff > 0 else ("-", "🔻")
    return f"{arrow} {sign}${abs(diff):,.2f} ({sign}{abs(percent):.2f}%) since {_clock(since)}"


def next_high_impact_event(db: EventRepository, now: datetime, tz: ZoneInfo | timezone, impact: str = "High"):
    """The next USD event of the given impact after `now`, with its instant, or None."""
    since = (now.astimezone(tz) - timedelta(days=1)).date().isoformat()
    upcoming = []
    for event in db.query_events(currency="USD", impacts=[impact], date_from=since):
        if not event.datetime_utc:
            continue
        try:
            instant = datetime.fromisoformat(event.datetime_utc.replace("Z", "+00:00"))
        except ValueError:
            continue
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        if instant > now:
            upcoming.append((instant, event))
    return min(upcoming, key=lambda pair: pair[0]) if upcoming else None


def next_key_event(db: EventRepository, now: datetime, tz: ZoneInfo | timezone):
    """The next high-impact USD event; in a week with none left, the next medium-impact one."""
    return next_high_impact_event(db, now, tz) or next_high_impact_event(db, now, tz, impact="Medium")


def build_pulse_text(*, now: datetime, quote: PriceQuote, previous: dict | None, upcoming, tz: ZoneInfo | timezone) -> str:
    """The pulse post. Deterministic: the same inputs always give the same text."""
    local = now.astimezone(tz)
    zone = local.strftime("%Z") or "UTC"
    lines = [
        "🟡 *GOLD MARKET PULSE*",
        f"🗓 {_day(local)} · {_clock(local)} {zone}",
        "",
        f"💰 *XAU/USD: ${quote.price:,.2f}*",
    ]
    if previous is not None:
        since = datetime.fromisoformat(previous["slot"].replace("Z", "+00:00"))
        if timedelta(0) < now - since <= MAX_COMPARE_AGE and previous["price"] > 0:
            lines.append(format_change(quote.price, float(previous["price"]), since.astimezone(tz)))
    lines.append("")
    if upcoming is None:
        lines += ["⏭ *Next USD event*", "No high or medium-impact event left on this week's calendar."]
    else:
        instant, event = upcoming
        # The impact is Forex Factory's own rating, stated as it is.
        lines.append(f"⏭ *Next {event.impact.lower()}-impact USD event*")
        when = instant.astimezone(tz)
        days_ahead = (when.date() - local.date()).days
        day = "today" if days_ahead == 0 else "tomorrow" if days_ahead == 1 else _day(when)
        lines.append(event.event_name)
        lines.append(f"🕒 {_clock(when)} {zone} {day} · {format_countdown(instant - now)}")
    lines += ["", DISCLAIMER]
    return "\n".join(lines)


# -- sending --------------------------------------------------------------------------------

def send_pulse(db: EventRepository, client: TelegramClient | None, settings: Settings, chat_id: str, *,
               now: datetime | None = None, dry_run: bool = False, fetch=fetch_gold_price) -> PulseResult:
    """Post the pulse for the current half-hour slot to Telegram, at most once."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if not market_is_open(now):
        return PulseResult(OUTCOME_MARKET_CLOSED)
    slot = slot_start(now)
    key = message_key(slot)
    existing = db.get_delivery(key, PROVIDER_TELEGRAM, chat_id)
    if existing is not None and existing.status == SENT:
        # Decided before asking the price source, so repeat runs in a slot cost it nothing.
        return PulseResult(OUTCOME_ALREADY_SENT, key, provider_message_id=existing.provider_message_id, sent_at=existing.sent_at)

    quote = fetch(settings.gold_price_url, timeout=settings.request_timeout_seconds, user_agent=settings.user_agent)
    if now - quote.updated_at > MAX_QUOTE_AGE:
        log.info("Pulse skipped: the price was last updated at %s", quote.updated_at.isoformat())
        return PulseResult(OUTCOME_STALE_PRICE, key)

    tz = ZoneInfo(settings.display_timezone) if settings.display_timezone else timezone.utc
    previous = db.latest_price_snapshot(SYMBOL, slot_key(slot))
    text = build_pulse_text(now=now, quote=quote, previous=previous,
                            upcoming=next_key_event(db, now, tz), tz=tz)
    result = deliver_text(
        db, client, message_key=key, message_type=MESSAGE_TYPE, text=text, chat_id=chat_id, dry_run=dry_run, now=now,
        provider=PROVIDER_TELEGRAM, destination=DESTINATION_TELEGRAM_CHANNEL, send_options={"markdown": True},
        label=describe_chat(chat_id))
    if result.outcome == OUTCOME_SENT:
        # Stored only for a post that went out: the next post's change line refers to what readers saw.
        db.save_price_snapshot(SYMBOL, slot_key(slot), quote.price, quote.source,
                               quote.updated_at.strftime("%Y-%m-%dT%H:%M:%SZ"), now.strftime("%Y-%m-%dT%H:%M:%SZ"))
        db.delete_price_snapshots_before(SYMBOL, slot_key(slot - KEEP_SNAPSHOTS))
    return PulseResult(result.outcome, key, text, result.provider_message_id, result.sent_at, result.error)


__all__ = [
    "OUTCOME_ALREADY_SENT", "OUTCOME_DRY_RUN", "OUTCOME_FAILED", "OUTCOME_MARKET_CLOSED", "OUTCOME_SENT",
    "OUTCOME_STALE_PRICE", "PriceError", "PriceQuote", "PulseResult", "build_pulse_text", "fetch_gold_price",
    "format_change", "format_countdown", "market_is_open", "message_key", "next_high_impact_event", "next_key_event", "parse_quote",
    "send_pulse", "slot_key", "slot_start",
]
