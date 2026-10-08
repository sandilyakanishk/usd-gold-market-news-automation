"""Trader's corner and learn cards: a quiz, a rule, a gold fact, a myth, a lesson.

Where the words come from, in this order:

  1. A free AI writer (Google Gemini), if GEMINI_API_KEY is set. It is asked
     for one new item on a topic that rotates through TOPICS, and told what
     was used lately so it does not repeat itself.
  2. The written library in config/content_library.json, taken in order with
     a bookmark per list. This is used whenever the AI is not configured, is
     unavailable, or its text fails a check, so a card always goes out.

Every item, from either source, passes the same checks before it is posted:
the right shape and length, and no trade recommendation, price prediction,
promise of profit, link or contact detail. This applies to education cards
only. News, prices, key levels and the recap never involve an AI.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import cards
from .config import Settings
from .database.base import EventRepository
from .delivery.models import DESTINATION_TELEGRAM_CHANNEL, OUTCOME_SENT, PROVIDER_TELEGRAM
from .delivery.service import deliver_text
from .delivery.telegram import TelegramClient, describe_chat

log = logging.getLogger(__name__)

KINDS = ("quiz", "rule", "fact", "myth", "lesson")
REMEMBER = 40  # how many recent items are kept to avoid repeats
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Subject areas the AI writer is walked through, one per request, so the cards cover the whole field.
TOPICS = (
    "what moves the gold price", "the US dollar and gold", "interest rates and gold", "inflation and CPI",
    "US jobs data and Non-Farm Payrolls", "the Federal Reserve and the FOMC", "bond yields", "the US Dollar Index",
    "risk management", "position sizing", "stop-loss orders", "leverage and margin", "spreads and trading costs",
    "trading psychology", "keeping a trading journal", "building a trading plan", "candlestick charts",
    "support and resistance", "trends and ranges", "moving averages", "trading sessions and market hours",
    "volatility", "liquidity and slippage", "economic calendars", "central bank gold reserves", "the history of gold as money",
    "gold purity and units of weight", "gold mining and supply", "jewellery and physical demand", "silver and other precious metals",
    "safe-haven assets", "GDP and economic growth", "PCE inflation", "retail sales and consumer spending",
    "order types", "pips, lots and contract sizes", "demo accounts and practice", "common beginner mistakes",
    "avoiding scams and false promises", "the foreign exchange market", "major currency pairs", "how brokers work",
    "risk-to-reward ratio", "drawdowns and recovery", "overtrading", "trading around news releases",
    "weekend gaps", "correlation between markets", "oil, stocks and gold", "geopolitics and gold",
    "pivot points", "timeframes", "backtesting an idea", "patience and discipline", "protecting capital",
    "hawkish and dovish central banks", "real interest rates", "exchange-traded gold funds", "futures and spot prices",
    "the gold-silver ratio",
)

# A card may explain what buying and selling are. It may never tell the reader to do either, or say where price is going.
_FORBIDDEN = re.compile(
    r"\b(buy now|sell now|you should (buy|sell)|time to (buy|sell)|must (buy|sell)|go (long|short) now"
    r"|will (rise|fall|go up|go down|rally|crash|drop|surge|hit|reach|soar|plunge)|is going to (rise|fall|hit|reach)"
    r"|price target|target of \$|guarantee[ds]? (profit|return|win)|sure[- ]?shot|risk[- ]free|can'?t lose|no risk"
    r"|double your|get rich|100 ?% (profit|accura|win)|dm me|whatsapp|telegram group|join (our|my)|sign up|subscribe"
    r"|this week|next week|today'?s? price|right now gold|currently trading at)\b"
    r"|https?://|www\.|@\w{3,}", re.I)
_MARKUP = re.compile(r"[*_`\[\]]")


class ContentError(Exception):
    """An item is not fit to post, or no item could be produced."""


# -- checks ---------------------------------------------------------------------------------

def _text(value: object, name: str, limit: int, minimum: int = 8) -> str:
    if not isinstance(value, str):
        raise ContentError(f"{name} is not text")
    cleaned = " ".join(_MARKUP.sub("", value).split())
    if not (minimum <= len(cleaned) <= limit):
        raise ContentError(f"{name} has {len(cleaned)} characters (allowed {minimum} to {limit})")
    if _FORBIDDEN.search(cleaned):
        raise ContentError(f"{name} reads like advice, a prediction or a promotion")
    return cleaned


def validate(kind: str, item: object) -> dict:
    """Return the item in clean form, or raise ContentError saying what is wrong with it."""
    if kind not in KINDS:
        raise ContentError(f"unknown kind {kind!r}")
    if not isinstance(item, dict):
        raise ContentError("the item is not an object")
    if kind == "quiz":
        options = item.get("options")
        if not isinstance(options, list) or len(options) != 4:
            raise ContentError("a quiz needs exactly four options")
        cleaned = [_text(o, "an option", 90, minimum=1) for o in options]
        if len({o.lower() for o in cleaned}) != 4:
            raise ContentError("two quiz options are the same")
        answer = item.get("answer")
        if isinstance(answer, bool) or not isinstance(answer, int) or not (0 <= answer < 4):
            raise ContentError("the quiz answer must be the position (0 to 3) of the correct option")
        return {"question": _text(item.get("question"), "the question", 250), "options": cleaned, "answer": answer,
                "explanation": _text(item.get("explanation"), "the explanation", 190)}
    if kind == "rule":
        return {"title": _text(item.get("title"), "the title", 70, minimum=4), "text": _text(item.get("text"), "the text", 320, minimum=30)}
    if kind == "fact":
        return {"text": _text(item.get("text"), "the text", 340, minimum=30)}
    if kind == "myth":
        return {"myth": _text(item.get("myth"), "the myth", 170), "truth": _text(item.get("truth"), "the truth", 340, minimum=30)}
    points = item.get("points")
    if not isinstance(points, list) or len(points) != 3:
        raise ContentError("a lesson needs exactly three points")
    return {"title": _text(item.get("title"), "the title", 75, minimum=4),
            "points": [_text(p, "a point", 220, minimum=15) for p in points],
            "takeaway": _text(item.get("takeaway"), "the takeaway", 170)}


def headline(kind: str, item: dict) -> str:
    """The short text an item is remembered by."""
    return {"quiz": item.get("question"), "rule": item.get("title"), "fact": item.get("text"),
            "myth": item.get("myth"), "lesson": item.get("title")}[kind] or ""


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


# -- library --------------------------------------------------------------------------------

def load_library(path: Path) -> dict[str, list[dict]]:
    """The written library. Every item is checked on load, so a bad edit is caught at once."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ContentError(f"The content library {path} could not be read: {exc}") from exc
    library = {}
    for kind in KINDS:
        items = raw.get(kind)
        if not isinstance(items, list) or not items:
            raise ContentError(f"The content library has no '{kind}' list.")
        try:
            library[kind] = [validate(kind, item) for item in items]
        except ContentError as exc:
            raise ContentError(f"The content library's '{kind}' list has a bad item: {exc}") from exc
    return library


# -- AI writer ------------------------------------------------------------------------------

_SHAPES = {
    "quiz": '{"question": "...", "options": ["...", "...", "...", "..."], "answer": 0, "explanation": "..."} '
            "(four short options, exactly one correct; answer is the 0-based position of the correct one; "
            "explanation under 180 characters)",
    "rule": '{"title": "...", "text": "..."} (a risk-management or discipline principle; title under 60 characters, text under 300)',
    "fact": '{"text": "..."} (one well-established, verifiable fact, under 300 characters)',
    "myth": '{"myth": "...", "truth": "..."} (a common misconception and the short correction; myth under 150 characters, truth under 300)',
    "lesson": '{"title": "...", "points": ["...", "...", "..."], "takeaway": "..."} '
              "(a beginner lesson: title under 60 characters, exactly three points under 200 characters each, takeaway under 150)",
}


def build_prompt(kind: str, topic: str, avoid: list[str]) -> str:
    lines = [
        "You write short educational cards for a Telegram channel about gold (XAU/USD) and US dollar trading.",
        "The readers are beginners. Write in plain, simple English.",
        f"Write ONE new {kind} about this subject: {topic}.",
        f"Reply with JSON only, in exactly this shape: {_SHAPES[kind]}",
        "Strict rules:",
        "- Education only. Never tell the reader to buy or sell, never predict where a price will go, never promise profit.",
        "- Only state things that are well established and timeless. No current prices, dates, recent events or statistics that change.",
        "- No links, no names of brokers or products, no emojis, no markdown, no hashtags.",
    ]
    if avoid:
        lines.append("- It must be clearly different from these, which were used recently: " + " | ".join(a[:90] for a in avoid[-15:]))
    return "\n".join(lines)


def ask_gemini(prompt: str, api_key: str, *, model: str, timeout: int = 20, user_agent: str = "") -> dict:
    """One request to Gemini. The key travels in a header and never appears in an error."""
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                       "generationConfig": {"responseMimeType": "application/json", "temperature": 0.9}}).encode("utf-8")
    request = urllib.request.Request(
        GEMINI_URL.format(model=model), data=body, method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key, "User-Agent": user_agent})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            reply = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        raise ContentError(f"the AI writer answered HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ContentError(f"the AI writer could not be reached ({type(exc).__name__})") from None
    except ValueError:
        raise ContentError("the AI writer's reply was not JSON") from None
    try:
        text = reply["candidates"][0]["content"]["parts"][0]["text"]
        item = json.loads(text)
    except (KeyError, IndexError, TypeError, ValueError):
        raise ContentError("the AI writer's reply had no usable item") from None
    if isinstance(item, list) and len(item) == 1:
        item = item[0]
    if not isinstance(item, dict):
        raise ContentError("the AI writer's reply was not one item")
    return item


def ask_any_model(prompt: str, settings: Settings, *, ask=ask_gemini) -> dict:
    """Ask each configured model in turn until one answers. Raises ContentError if none does."""
    models = [m.strip() for m in str(settings.gemini_model).split(",") if m.strip()]
    problems = []
    for model in models:
        try:
            return ask(prompt, settings.gemini_api_key, model=model, timeout=settings.request_timeout_seconds,
                       user_agent=settings.user_agent)
        except ContentError as exc:
            problems.append(f"{model}: {exc}")
    raise ContentError("no AI model answered (" + "; ".join(problems) + ")" if problems else "no AI model is configured")


# -- choosing the next item -----------------------------------------------------------------

def _state(db: EventRepository, kind: str) -> tuple[int, list[str], int]:
    """(library bookmark, recent headlines, how many AI items were made) for a kind."""
    stored = db.get_content_state(kind)
    if stored is None:
        return 0, [], 0
    try:
        memory = json.loads(stored["recent"])
        recent, made = list(memory.get("recent", [])), int(memory.get("ai_made", 0))
    except (ValueError, TypeError, AttributeError):
        recent, made = [], 0
    return stored["position"], recent, made


def next_item(db: EventRepository, kind: str, library: dict, settings: Settings, *, ask=ask_gemini) -> tuple[dict, str, dict]:
    """The item to post next: (item, source, new state). Nothing is saved here."""
    position, recent, made = _state(db, kind)
    seen = {_norm(r) for r in recent}
    if getattr(settings, "gemini_api_key", None):
        topic = TOPICS[made % len(TOPICS)]
        try:
            raw = ask_any_model(build_prompt(kind, topic, recent), settings, ask=ask)
            item = validate(kind, raw)
            if _norm(headline(kind, item)) in seen:
                raise ContentError("it repeats a recent item")
        except ContentError as exc:
            log.warning("AI writer not used for %s (%s); taking the next library item.", kind, exc)
            made += 1  # move to the next topic even so, so one awkward topic cannot block the writer
        else:
            return item, "ai", {"position": position, "recent": recent + [headline(kind, item)], "ai_made": made + 1}
    items = library[kind]
    item = items[position % len(items)]  # in order; after the last one the list starts again
    return item, "library", {"position": position + 1, "recent": recent + [headline(kind, item)], "ai_made": made}


def save_state(db: EventRepository, kind: str, state: dict, now: datetime) -> None:
    memory = json.dumps({"recent": state["recent"][-REMEMBER:], "ai_made": state["ai_made"]}, ensure_ascii=False)
    db.save_content_state(kind, state["position"], memory, now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))


# -- text -----------------------------------------------------------------------------------

def build_text(kind: str, item: dict) -> str:
    """Each kind has a look of its own."""
    if kind == "rule":
        return f"🛡 *TRADER'S RULE*\n\n*{item['title']}*\n{item['text']}\n\n_Protect the account first._"
    if kind == "fact":
        return f"💡 *DID YOU KNOW?*\n\n{item['text']}"
    if kind == "myth":
        return f"⚖️ *MYTH OR FACT?*\n\n❌ *Myth:* {item['myth']}\n\n✅ *Truth:* {item['truth']}"
    if kind == "lesson":
        one, two, three = item["points"]
        return (f"📘 *LEARN: {item['title'].upper()}*\n\n1️⃣ {one}\n\n2️⃣ {two}\n\n3️⃣ {three}\n\n"
                f"🎯 *Remember:* {item['takeaway']}")
    return f"❓ Quiz: {item['question']}"


def arrange_quiz(item: dict) -> tuple[list[str], int]:
    """The options in a settled order that depends on the question, so the right answer is not always in one place."""
    shift = int(hashlib.sha1(item["question"].encode("utf-8")).hexdigest(), 16) % 4
    options = item["options"][shift:] + item["options"][:shift]
    return options, (item["answer"] - shift) % 4


class _QuizPost:
    def __init__(self, client: TelegramClient, item: dict):
        self._client, self._item = client, item

    def send_text(self, chat_id: str, text: str, **_: object) -> str:
        options, correct = arrange_quiz(self._item)
        return self._client.send_quiz(chat_id, text, options, correct, self._item["explanation"])


# -- sending --------------------------------------------------------------------------------

def send_content_card(db: EventRepository, client: TelegramClient | None, settings: Settings, chat_id: str, slot: cards.Slot,
                      library: dict, *, now: datetime, dry_run: bool = False, ask=ask_gemini) -> cards.CardResult:
    """Post the trader's corner or learn card for a slot."""
    kind = slot.variant if slot.kind == cards.CORNER else "lesson"
    item, source, state = next_item(db, kind, library, settings, ask=ask)
    text = build_text(kind, item)
    poster = client
    if kind == "quiz" and client is not None:
        poster = _QuizPost(client, item)
    result = deliver_text(
        db, poster, message_key=slot.message_key, message_type=slot.kind, text=text, chat_id=chat_id, dry_run=dry_run,
        now=now, provider=PROVIDER_TELEGRAM, destination=DESTINATION_TELEGRAM_CHANNEL,
        send_options=None if kind == "quiz" else {"markdown": True}, label=describe_chat(chat_id))
    if result.outcome == OUTCOME_SENT:
        save_state(db, kind, state, now)
    shown = text if kind != "quiz" else text + "\n" + "\n".join(
        f"  {'✔' if i == arrange_quiz(item)[1] else '•'} {o}" for i, o in enumerate(arrange_quiz(item)[0])) + f"\n  ({item['explanation']})"
    return cards.CardResult(slot.kind, result.outcome, slot.message_key, shown, result.provider_message_id,
                            result.error or f"{kind} from the {source}")


__all__ = [
    "ContentError", "KINDS", "TOPICS", "arrange_quiz", "ask_gemini", "build_prompt", "build_text", "headline",
    "load_library", "next_item", "save_state", "send_content_card", "validate",
]
