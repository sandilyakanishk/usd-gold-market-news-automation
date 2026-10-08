"""The daily timetable of scheduled Telegram cards, and the cards built from recorded gold prices.

    python -m src.main --telegram-send-scheduled [--dry-run]

Run every 10 minutes. Each run records the gold price, then posts whichever
card is due at that moment and has not been posted yet. Times are in the
display timezone (India time):

    08:30            key levels of the day     (Monday to Friday)
    09:00 ... 23:00  market pulse, every 2 h   (Monday to Friday)
    10:00 ... 22:00  trader's corner, every 2 h, one hour after each pulse
    11:30            what's moving gold        (Monday to Friday)
    15:30            learn card
    23:30            daily recap               (Monday to Friday)

Nothing is scheduled between midnight and 08:30. A card's slot is its message
identity, so however often the automation runs, each slot is posted once. A
run that arrives late still posts the card, up to GRACE after its time.

Every card here is arithmetic on real prices and a fixed template. Nothing is
a trading view.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from . import pulse
from .config import Settings
from .database.base import EventRepository
from .delivery.models import (
    DESTINATION_TELEGRAM_CHANNEL, OUTCOME_ALREADY_SENT, OUTCOME_DRY_RUN, OUTCOME_FAILED, OUTCOME_SENT,
    PROVIDER_TELEGRAM, SENT,
)
from .delivery.service import deliver_text
from .delivery.telegram import TelegramClient, describe_chat

log = logging.getLogger(__name__)

SYMBOL = pulse.SYMBOL
PULSE, CORNER, KEY_LEVELS, DRIVERS, LEARN, RECAP = "MARKET_PULSE", "TRADER_CORNER", "KEY_LEVELS", "GOLD_DRIVERS", "LEARN_CARD", "DAILY_RECAP"
COUNTDOWN = "NEWS_COUNTDOWN"
# A countdown goes out when a high-impact USD release is this many minutes away.
COUNTDOWN_FROM, COUNTDOWN_UNTIL = 35, 5
# The official daily US figures shown on the "what's moving gold" card: (FRED series, label, unit, decimals).
DRIVER_SERIES = (
    ("DTWEXBGS", "💵 US dollar index (broad)", "", 2),
    ("DGS10", "📈 US 10-year bond yield", "%", 2),
    ("DFEDTARU", "🏦 Fed interest rate (upper limit)", "%", 2),
)
PULSE_HOURS = (9, 11, 13, 15, 17, 19, 21, 23)
# One hour after each pulse; the kind of card rotates through the day.
CORNER_SLOTS = ((10, "quiz"), (12, "rule"), (14, "fact"), (16, "myth"), (18, "quiz"), (20, "rule"), (22, "fact"))
FIXED_SLOTS = ((KEY_LEVELS, time(8, 30)), (DRIVERS, time(11, 30)), (LEARN, time(15, 30)), (RECAP, time(23, 30)))
# Cards that only make sense on a day the gold market trades.
MARKET_DAY_ONLY = (PULSE, KEY_LEVELS, DRIVERS, RECAP)
GRACE = timedelta(minutes=50)
SAMPLE_MINUTES = 10
MIN_SAMPLES = 6  # fewer recorded prices than this do not describe a day
MIN_LEVEL_SAMPLES = 72  # key levels need most of a day (12 hours of 10-minute prices), or the range would mislead
KEEP_DAILY = 10  # days of daily summaries kept for the key levels
OUTCOME_NOT_READY = "NOT_READY"
STAMP = "%Y-%m-%dT%H:%M:%SZ"
NOTE = "ℹ️ Arithmetic on recorded prices. Information only, not trading advice."


@dataclass(frozen=True)
class Slot:
    kind: str
    start: datetime  # in the display timezone
    variant: str | None = None

    @property
    def message_key(self) -> str:
        return f"{self.kind}_{self.start.strftime('%Y-%m-%d_%H%M')}"


@dataclass(frozen=True)
class CardResult:
    kind: str
    outcome: str
    message_key: str | None = None
    text: str | None = None
    provider_message_id: str | None = None
    detail: str | None = None


def clock() -> datetime:
    """The current moment. One place, so a whole run agrees on what time it is."""
    return datetime.now(timezone.utc)


# -- timetable ------------------------------------------------------------------------------

def slots_for(day: date, tz: ZoneInfo | timezone) -> list[Slot]:
    """Every scheduled card of one day, in time order."""
    def at(moment: time) -> datetime:
        return datetime.combine(day, moment, tzinfo=tz)
    slots = [Slot(PULSE, at(time(hour))) for hour in PULSE_HOURS]
    slots += [Slot(CORNER, at(time(hour)), kind) for hour, kind in CORNER_SLOTS]
    slots += [Slot(kind, at(moment)) for kind, moment in FIXED_SLOTS]
    if day.weekday() >= 5:  # Saturday, Sunday
        slots = [s for s in slots if s.kind not in MARKET_DAY_ONLY]
    return sorted(slots, key=lambda s: s.start)


def due_slots(now: datetime, tz: ZoneInfo | timezone) -> list[Slot]:
    """The cards whose time has come and whose grace period has not run out."""
    local = now.astimezone(tz)
    end_of_day = datetime.combine(local.date() + timedelta(days=1), time(0), tzinfo=tz)
    return [s for s in slots_for(local.date(), tz) if s.start <= local < min(s.start + GRACE, end_of_day)]


# -- recorded prices ------------------------------------------------------------------------

def sample_slot(now: datetime) -> str:
    utc = now.astimezone(timezone.utc)
    return utc.replace(minute=utc.minute - utc.minute % SAMPLE_MINUTES, second=0, microsecond=0).strftime(STAMP)


def day_bounds(day: date, tz: ZoneInfo | timezone) -> tuple[str, str]:
    """The UTC instants (as stored text) at which a local day starts and ends."""
    start = datetime.combine(day, time(0), tzinfo=tz)
    return start.astimezone(timezone.utc).strftime(STAMP), (start + timedelta(days=1)).astimezone(timezone.utc).strftime(STAMP)


def record_price(db: EventRepository, quote: pulse.PriceQuote, now: datetime) -> None:
    """Keep the current price as one sample. Feeds the pulse's change line, the recap and the key levels."""
    db.save_price_snapshot(SYMBOL, sample_slot(now), quote.price, quote.source,
                           quote.updated_at.strftime(STAMP), now.astimezone(timezone.utc).strftime(STAMP))


def summarize(samples: list[dict]) -> dict | None:
    """Open, high, low and close of a list of samples in time order, or None if there are too few."""
    if len(samples) < MIN_SAMPLES:
        return None
    prices = [float(s["price"]) for s in samples]
    return {"open": prices[0], "high": max(prices), "low": min(prices), "close": prices[-1], "samples": len(prices)}


def roll_prices(db: EventRepository, now: datetime, tz: ZoneInfo | timezone) -> int:
    """Turn the samples of past days into one summary line each, then delete those samples.

    Returns how many samples were deleted. Today's samples are never touched.
    """
    today = now.astimezone(tz).date()
    today_start, _ = day_bounds(today, tz)
    old = db.price_snapshots_between(SYMBOL, "0000", today_start)
    by_day: dict[date, list[dict]] = {}
    for sample in old:
        moment = datetime.strptime(sample["slot"], STAMP).replace(tzinfo=timezone.utc).astimezone(tz)
        by_day.setdefault(moment.date(), []).append(sample)
    for day, samples in by_day.items():
        summary = summarize(samples)
        if summary is not None:
            db.save_daily_price(SYMBOL, day.isoformat(), summary["open"], summary["high"], summary["low"],
                                summary["close"], summary["samples"])
    removed = db.delete_price_snapshots_before(SYMBOL, today_start)
    db.delete_daily_prices_before(SYMBOL, (today - timedelta(days=KEEP_DAILY)).isoformat())
    return removed


# -- text -----------------------------------------------------------------------------------

def _money(value: float) -> str:
    return f"{value:,.2f}"


def pivot_levels(high: float, low: float, close: float) -> dict[str, float]:
    """Classic floor-trader pivots."""
    pivot = (high + low + close) / 3
    return {"R2": pivot + (high - low), "R1": 2 * pivot - low, "P": pivot, "S1": 2 * pivot - high, "S2": pivot - (high - low)}


def build_key_levels(today: date, daily: dict) -> str:
    levels = pivot_levels(daily["high"], daily["low"], daily["close"])
    source_day = date.fromisoformat(daily["day"])
    width = max(len(_money(v)) for v in levels.values())
    rows = [("🔴", "R2   ", "R2"), ("🟠", "R1   ", "R1"), ("⚪", "Pivot", "P"), ("🟢", "S1   ", "S1"), ("🟢", "S2   ", "S2")]
    ladder = [f"{dot} {label}  {_money(levels[name]):>{width}}" for dot, label, name in rows]
    return "\n".join([
        f"🗺 *KEY LEVELS* · {pulse._day(datetime.combine(today, time(0)))}",
        f"XAU/USD · from {source_day.strftime('%A')}'s range",
        "",
        "```",
        *ladder,
        "```",
        f"{source_day.strftime('%A')}: High {_money(daily['high'])} · Low {_money(daily['low'])} · Close {_money(daily['close'])}",
        "",
        "ℹ️ Levels are arithmetic on recorded prices, not trading advice.",
    ])


def build_recap(today: date, summary: dict) -> str:
    change = round(summary["close"] - summary["open"], 2)
    percent = change / summary["open"] * 100
    if change == 0:
        move = "▪️ Unchanged on the day"
    else:
        sign, arrow = ("+", "🔺") if change > 0 else ("-", "🔻")
        move = f"{arrow} {sign}${abs(change):,.2f} ({sign}{abs(percent):.2f}%) on the day"
    return "\n".join([
        f"📊 *DAILY RECAP* · {pulse._day(datetime.combine(today, time(0)))}",
        "XAU/USD",
        "",
        "```",
        f"Open  ${_money(summary['open'])}",
        f"High  ${_money(summary['high'])}",
        f"Low   ${_money(summary['low'])}",
        f"Last  ${_money(summary['close'])}",
        "```",
        move,
        f"📏 Day's range: ${_money(summary['high'] - summary['low'])}",
        "",
        NOTE,
    ])


def build_countdown(event, instant: datetime, now: datetime, tz: ZoneInfo | timezone) -> str:
    minutes = max(1, round((instant - now).total_seconds() / 60))
    when = instant.astimezone(tz)
    lines = [
        f"⏰ *NEWS IN {minutes} MINUTES*",
        "",
        f"*{event.event_name}*",
        f"USD · {event.impact.lower()} impact · {pulse._clock(when)} {when.strftime('%Z') or 'UTC'}",
    ]
    figures = [f"{label}: {value}" for label, value in (("Forecast", event.forecast), ("Previous", event.previous)) if value]
    if figures:
        lines += ["", "   ".join(figures)]
    lines += ["", "⚠️ Prices can move fast and spreads can widen around the release.", "ℹ️ Information only, not trading advice."]
    return "\n".join(lines)


def _move(latest, earlier, unit: str, decimals: int) -> str:
    change = round(float(latest) - float(earlier), decimals)
    if change == 0:
        return "▪️ unchanged"
    return f"{'🔺 +' if change > 0 else '🔻 -'}{abs(change):.{decimals}f}{unit}"


def build_drivers(today: date, readings: list[tuple]) -> str:
    """`readings`: (label, unit, decimals, latest date, latest value, previous value or None) per figure."""
    lines = [f"🧭 *WHAT'S MOVING GOLD* · {pulse._day(datetime.combine(today, time(0)))}", "Latest official US figures", ""]
    for label, unit, decimals, day, latest, earlier in readings:
        move = f"  {_move(latest, earlier, unit, decimals)}" if earlier is not None else ""
        lines.append(f"{label}\n   {float(latest):,.{decimals}f}{unit}{move}  ({day.day} {day.strftime('%b')})")
    lines += ["", "_A stronger dollar and higher yields have usually weighed on gold, and the reverse has usually supported it._",
              "ℹ️ Published once a day by FRED, so a day or more old. Not trading advice."]
    return "\n".join(lines)


def read_drivers(settings: Settings, today: date, *, provider=None) -> list[tuple]:
    """The latest two published values of each figure, from FRED. Raises ProviderError if they cannot be had."""
    from .actuals.providers import FREDProvider, ProviderError, SeriesRequest
    provider = provider or FREDProvider(settings.fred_api_key, timeout=settings.request_timeout_seconds, user_agent=settings.user_agent)
    reason = provider.unavailable_reason()
    if reason:
        raise ProviderError(reason)
    observations = provider.fetch([SeriesRequest(series, today - timedelta(days=45), today) for series, *_ in DRIVER_SERIES])
    readings = []
    for series, label, unit, decimals in DRIVER_SERIES:
        days = sorted(observations.get(series) or {})
        if not days:
            continue
        values = observations[series]
        readings.append((label, unit, decimals, days[-1], values[days[-1]], values[days[-2]] if len(days) > 1 else None))
    if not readings:
        raise ProviderError("FRED returned no recent figures.")
    return readings


# -- sending --------------------------------------------------------------------------------

def _already(db: EventRepository, key: str, chat_id: str):
    record = db.get_delivery(key, PROVIDER_TELEGRAM, chat_id)
    return record if record is not None and record.status == SENT else None


def _post(db, client, chat_id, slot: Slot, text: str, *, dry_run: bool, now: datetime) -> CardResult:
    result = deliver_text(
        db, client, message_key=slot.message_key, message_type=slot.kind, text=text, chat_id=chat_id, dry_run=dry_run,
        now=now, provider=PROVIDER_TELEGRAM, destination=DESTINATION_TELEGRAM_CHANNEL, send_options={"markdown": True},
        label=describe_chat(chat_id))
    return CardResult(slot.kind, result.outcome, slot.message_key, text, result.provider_message_id, result.error)


def _pulse_card(db, client, settings, chat_id, slot, quote, *, now, tz, dry_run) -> CardResult:
    if quote is None:
        return CardResult(PULSE, OUTCOME_NOT_READY, slot.message_key, detail="no current price")
    earlier = db.latest_price_snapshot(SYMBOL, sample_slot(now - timedelta(minutes=110)))
    if earlier is not None:
        age = now - datetime.strptime(earlier["slot"], STAMP).replace(tzinfo=timezone.utc)
        if age > timedelta(hours=3):
            earlier = None  # too old to be "since the last pulse"
    text = pulse.build_pulse_text(now=now, quote=quote, previous=earlier, upcoming=pulse.next_key_event(db, now, tz), tz=tz)
    return _post(db, client, chat_id, slot, text, dry_run=dry_run, now=now)


def _key_levels_card(db, client, chat_id, slot, *, now, tz, dry_run) -> CardResult:
    today = now.astimezone(tz).date()
    daily = db.latest_daily_price(SYMBOL, today.isoformat())
    if daily is None or (today - date.fromisoformat(daily["day"])).days > 4 or daily["samples"] < MIN_LEVEL_SAMPLES:
        return CardResult(KEY_LEVELS, OUTCOME_NOT_READY, slot.message_key, detail="no full recent day of recorded prices yet")
    return _post(db, client, chat_id, slot, build_key_levels(today, daily), dry_run=dry_run, now=now)


def _recap_card(db, client, chat_id, slot, *, now, tz, dry_run) -> CardResult:
    today = now.astimezone(tz).date()
    start, end = day_bounds(today, tz)
    summary = summarize(db.price_snapshots_between(SYMBOL, start, end))
    if summary is None:
        return CardResult(RECAP, OUTCOME_NOT_READY, slot.message_key, detail="too few prices recorded today")
    return _post(db, client, chat_id, slot, build_recap(today, summary), dry_run=dry_run, now=now)


def _drivers_card(db, client, settings, chat_id, slot, *, now, tz, dry_run, provider=None) -> CardResult:
    from .actuals.providers import ProviderError
    today = now.astimezone(tz).date()
    try:
        readings = read_drivers(settings, today, provider=provider)
    except ProviderError as exc:
        return CardResult(DRIVERS, OUTCOME_NOT_READY, slot.message_key, detail=str(exc))
    return _post(db, client, chat_id, slot, build_drivers(today, readings), dry_run=dry_run, now=now)


def _countdowns(db, client, chat_id, *, now, tz, dry_run) -> list[CardResult]:
    """One reminder per high-impact USD release that is about half an hour away."""
    results = []
    since = (now.astimezone(tz) - timedelta(days=1)).date().isoformat()
    for event in db.query_events(currency="USD", impacts=["High"], date_from=since):
        if not event.datetime_utc:
            continue
        try:
            instant = datetime.fromisoformat(event.datetime_utc.replace("Z", "+00:00"))
        except ValueError:
            continue
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        minutes = (instant - now).total_seconds() / 60
        if not (COUNTDOWN_UNTIL <= minutes <= COUNTDOWN_FROM):
            continue
        key = f"{COUNTDOWN}_{event.event_id}"
        if _already(db, key, chat_id) is not None:
            results.append(CardResult(COUNTDOWN, OUTCOME_ALREADY_SENT, key))
            continue
        text = build_countdown(event, instant, now, tz)
        result = deliver_text(
            db, client, message_key=key, message_type=COUNTDOWN, text=text, chat_id=chat_id, dry_run=dry_run, now=now,
            provider=PROVIDER_TELEGRAM, destination=DESTINATION_TELEGRAM_CHANNEL, send_options={"markdown": True},
            label=describe_chat(chat_id))
        results.append(CardResult(COUNTDOWN, result.outcome, key, text, result.provider_message_id, result.error))
    return results


def send_scheduled(db: EventRepository, client: TelegramClient | None, settings: Settings, chat_id: str, *,
                   now: datetime | None = None, dry_run: bool = False, fetch=pulse.fetch_gold_price,
                   extra_cards: dict | None = None, drivers_provider=None) -> list[CardResult]:
    """Record the price, then post every card that is due and not yet posted.

    `extra_cards` maps a card kind to a function (slot) -> CardResult for the
    kinds built elsewhere (trader's corner and learn card, in content_cards).
    """
    now = (now or clock()).astimezone(timezone.utc)
    tz = ZoneInfo(settings.display_timezone) if settings.display_timezone else timezone.utc
    results: list[CardResult] = []

    quote = None
    if pulse.market_is_open(now):
        try:
            fetched = fetch(settings.gold_price_url, timeout=settings.request_timeout_seconds, user_agent=settings.user_agent)
        except pulse.PriceError as exc:
            log.warning("Scheduled cards: %s", exc)
            results.append(CardResult("PRICE", OUTCOME_FAILED, detail=str(exc)))
        else:
            if now - fetched.updated_at <= pulse.MAX_QUOTE_AGE:
                quote = fetched
                if not dry_run:
                    record_price(db, quote, now)
    if not dry_run and now.astimezone(tz).time() >= time(8, 15):
        roll_prices(db, now, tz)

    results += _countdowns(db, client, chat_id, now=now, tz=tz, dry_run=dry_run)

    for slot in due_slots(now, tz):
        if _already(db, slot.message_key, chat_id) is not None:
            results.append(CardResult(slot.kind, OUTCOME_ALREADY_SENT, slot.message_key))
            continue
        if slot.kind == PULSE:
            results.append(_pulse_card(db, client, settings, chat_id, slot, quote, now=now, tz=tz, dry_run=dry_run))
        elif slot.kind == KEY_LEVELS:
            results.append(_key_levels_card(db, client, chat_id, slot, now=now, tz=tz, dry_run=dry_run))
        elif slot.kind == RECAP:
            results.append(_recap_card(db, client, chat_id, slot, now=now, tz=tz, dry_run=dry_run))
        elif slot.kind == DRIVERS:
            results.append(_drivers_card(db, client, settings, chat_id, slot, now=now, tz=tz, dry_run=dry_run,
                                         provider=drivers_provider))
        elif extra_cards and slot.kind in extra_cards:
            results.append(extra_cards[slot.kind](slot))
        else:
            results.append(CardResult(slot.kind, OUTCOME_NOT_READY, slot.message_key, detail="this card is not built yet"))
    return results


__all__ = [
    "CORNER", "COUNTDOWN", "CardResult", "DRIVERS", "build_countdown", "build_drivers", "read_drivers", "GRACE", "KEY_LEVELS", "LEARN", "OUTCOME_ALREADY_SENT", "OUTCOME_DRY_RUN",
    "OUTCOME_FAILED", "OUTCOME_NOT_READY", "OUTCOME_SENT", "PULSE", "RECAP", "Slot", "build_key_levels", "build_recap",
    "day_bounds", "due_slots", "pivot_levels", "record_price", "roll_prices", "sample_slot", "send_scheduled",
    "slots_for", "summarize",
]
