"""Typed trade alerts (Vercel serverless function): Telegram sends the owner's private messages to the bot here.

The owner types a trade to the bot in a private chat; this posts it, formatted,
to the Telegram channel and the WhatsApp Channel, and replies with a
confirmation. Later steps of the same trade are typed as a reply to that
confirmation, so nothing has to be remembered between messages:

    buy xauusd 4120.5 sl 4110 tp 4140        a new trade (sell works the same way)
    buy limit xauusd 4110 sl 4100 tp 4140    a pending order (limit or stop)

    reply to the bot's confirmation with:
    tp | sl                                  closed at the take-profit / stop-loss
    close 4135                               closed by hand at 4135
    be                                       stop-loss moved to the entry price
    move sl 4125 | move tp 4150              new stop-loss / take-profit
    filled                                   a pending order has been triggered
    cancel                                   a pending order has been cancelled

Only messages from OWNER_TELEGRAM_ID are acted on, and only requests carrying
Telegram's secret header (TELEGRAM_WEBHOOK_SECRET) are accepted.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import trade  # noqa: E402  (the shared message format and senders)

TAG = re.compile(r"^Trade: (?P<side>BUY|SELL|BUY LIMIT|SELL LIMIT|BUY STOP|SELL STOP) (?P<symbol>[A-Z0-9._#]{2,20}) "
                 r"(?P<price>[0-9.]+)(?: \| SL (?P<sl>[0-9.]+|none))?(?: \| TP (?P<tp>[0-9.]+|none))?$", re.M)
NEW = re.compile(r"^(?P<side>buy|sell)(?: (?P<kind>limit|stop))? (?P<symbol>[a-z0-9._#]{2,20}) (?:@ ?)?(?P<price>[0-9]+(?:\.[0-9]+)?)"
                 r"(?: sl (?P<sl>[0-9]+(?:\.[0-9]+)?))?(?: tp (?P<tp>[0-9]+(?:\.[0-9]+)?))?$")
HELP = ("Send a trade like:\n  buy xauusd 4120.5 sl 4110 tp 4140\n  buy limit xauusd 4110 sl 4100 tp 4140\n\n"
        "Then reply to my confirmation with: tp · sl · close 4135 · be · move sl 4125 · move tp 4150 · filled · cancel")


def point_size(symbol: str) -> float:
    if "XAU" in symbol or "XAG" in symbol:
        return 0.01
    if "JPY" in symbol:
        return 0.001
    return 0.00001


def tag(side: str, symbol: str, price: float, sl, tp) -> str:
    fmt = lambda v: "none" if v in (None, 0) else f"{v:g}"
    return f"Trade: {side} {symbol} {price:g} | SL {fmt(sl)} | TP {fmt(tp)}"


def _num(value):
    return None if value in (None, "none") else float(value)


def parse_new(text: str) -> dict | None:
    match = NEW.match(" ".join(text.lower().split()))
    if not match:
        return None
    side = match["side"].upper() + (f" {match['kind'].upper()}" if match["kind"] else "")
    return {"event": "pending_placed" if match["kind"] else "opened", "symbol": match["symbol"].upper(), "side": side,
            "price": float(match["price"]), "sl": _num(match["sl"]), "tp": _num(match["tp"])}


def parse_reply(text: str, original: str) -> tuple[dict | None, str | None]:
    """(event, error) for a reply to one of the bot's confirmations."""
    found = TAG.search(original or "")
    if not found:
        return None, "Reply to one of my trade confirmations (the message that ends with a 'Trade:' line)."
    side, symbol, price = found["side"], found["symbol"], float(found["price"])
    sl, tp = _num(found["sl"]), _num(found["tp"])
    words = " ".join(text.lower().split())
    pending = side not in ("BUY", "SELL")
    if pending:
        if words == "cancel":
            return {"event": "pending_cancelled", "symbol": symbol, "side": side, "price": price}, None
        if words == "filled":
            return {"event": "opened", "symbol": symbol, "side": side.split()[0], "price": price, "sl": sl, "tp": tp}, None
        return None, "This is a pending order: reply 'filled' when it triggers, or 'cancel'."
    base = {"symbol": symbol, "side": side}
    direction = 1 if side == "BUY" else -1

    def closed(exit_price: float, reason: str) -> dict:
        points = round((exit_price - price) * direction / point_size(symbol))
        return {**base, "event": "closed", "open_price": price, "close_price": exit_price, "points": points, "reason": reason}
    if words in ("tp", "tp hit"):
        return (closed(tp, "tp"), None) if tp else (None, "This trade has no TP; use 'close <price>'.")
    if words in ("sl", "sl hit"):
        return (closed(sl, "sl"), None) if sl else (None, "This trade has no SL; use 'close <price>'.")
    match = re.fullmatch(r"close (?:@ ?)?([0-9]+(?:\.[0-9]+)?)", words)
    if match:
        return closed(float(match[1]), "manual"), None
    if words in ("be", "breakeven"):
        return {**base, "event": "modified", "price": price, "sl": price, "tp": tp, "open_price": price}, None
    match = re.fullmatch(r"move (sl|tp) ([0-9]+(?:\.[0-9]+)?)", words)
    if match:
        new_sl, new_tp = (float(match[2]), tp) if match[1] == "sl" else (sl, float(match[2]))
        return {**base, "event": "modified", "price": price, "sl": new_sl, "tp": new_tp, "open_price": price}, None
    return None, "I did not understand that. Reply with: tp · sl · close 4135 · be · move sl 4125 · move tp 4150"


def confirmation(event: dict) -> str | None:
    """The private reply: what was posted, and the tag that later replies are read from. None when the trade is finished."""
    kind = event["event"]
    if kind in ("closed", "pending_cancelled"):
        return None
    sl, tp = event.get("sl"), event.get("tp")
    return tag(event["side"], event["symbol"], event["price"], sl, tp)


def reply_private(chat_id, text: str, env, reply_to: int | None = None) -> None:
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return
    payload = {"chat_id": chat_id, "text": text}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    trade._post_json(f"https://api.telegram.org/bot{token}/sendMessage", payload, {}, (token,))


def handle_update(update: dict, env, *, senders=None, reply=reply_private, now=None) -> str:
    """What happened with one Telegram update (for the log and the tests)."""
    message = (update or {}).get("message") or {}
    sender = (message.get("from") or {}).get("id")
    chat = (message.get("chat") or {}).get("id")
    text = (message.get("text") or "").strip()
    owner = str(env.get("OWNER_TELEGRAM_ID", "")).strip()
    if not owner or str(sender) != owner or (message.get("chat") or {}).get("type") != "private":
        return "ignored"
    if not text:
        return "ignored"
    original = (message.get("reply_to_message") or {}).get("text") or ""
    if original:
        event, error = parse_reply(text, original)
    else:
        event = parse_new(text)
        error = None if event else HELP
    if event is None:
        reply(chat, error, env, message.get("message_id"))
        return "help"
    try:
        post = trade.build_message(event, now)
    except trade.BadEvent as exc:
        reply(chat, f"That trade looks wrong ({exc}).", env, message.get("message_id"))
        return "rejected"
    telegram, whatsapp = senders or (trade.send_telegram, trade.send_whatsapp)
    tg_ok, _ = telegram(post, env)
    wa_ok, _ = whatsapp(post, env)
    status = f"Posted to Telegram {'✅' if tg_ok else '❌'} · WhatsApp {'✅' if wa_ok else '❌'}"
    follow = confirmation(event)
    if follow:
        status += "\n\nReply to this message with: " + ("filled · cancel" if event["event"] == "pending_placed" else
                                                       "tp · sl · close 4135 · be · move sl 4125 · move tp 4150")
        status += "\n" + follow
    reply(chat, status, env, message.get("message_id"))
    return "posted" if (tg_ok or wa_ok) else "failed"


class handler(BaseHTTPRequestHandler):  # Vercel's Python runtime looks for this name
    def _reply(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._reply(200, {"ok": True, "service": "typed trade alerts"})

    def do_POST(self):
        expected = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
        given = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if len(expected) < 16 or not hmac.compare_digest(given, expected):
            self._reply(401, {"ok": False})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            update = json.loads(self.rfile.read(min(length, 100_000)).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._reply(200, {"ok": True})   # never make Telegram retry a message it cannot read
            return
        self._reply(200, {"ok": True, "result": handle_update(update, os.environ)})
