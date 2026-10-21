"""Trade alerts receiver (Vercel serverless function): POST /api/trade

The MT5 add-on (mt5/TradeAlerts.mq5) sends one JSON event per trade action:
a pending order placed or cancelled, a position opened, its SL/TP changed, or
the position closed. This function checks the shared secret, turns the event
into a short message and posts it to the Telegram channel and the WhatsApp
Channel at once.

Shown: symbol, direction, prices, SL/TP and the result in price and points.
Never shown: lot size, account balance or money.

Settings (Vercel environment variables, never in code): TRADE_WEBHOOK_SECRET,
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, WHAPI_TOKEN, WHATSAPP_CHAT_ID, and
optionally WHAPI_BASE_URL. Only the standard library is used.
"""

from __future__ import annotations

import hmac
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler

IST = timezone(timedelta(hours=5, minutes=30))
USER_AGENT = "usd-gold-calendar-collector/0.1 (personal, low-frequency)"
DISCLAIMER = "⚠️ My personal trade, shared for transparency. Not financial advice."
KINDS = ("pending_placed", "pending_cancelled", "opened", "modified", "closed")
MAX_BODY = 10_000


class BadEvent(ValueError):
    """The event is missing something or holds a value that makes no sense."""


# -- the message ---------------------------------------------------------------------------

def _price(value) -> str:
    number = float(value)
    return f"{number:,.2f}" if abs(number) >= 100 else f"{number:,.5f}".rstrip("0").rstrip(".")


def _levels(event: dict) -> str:
    parts = []
    for name in ("sl", "tp"):
        value = event.get(name)
        parts.append(f"{name.upper()} {_price(value)}" if value not in (None, 0, 0.0, "") else f"{name.upper()} none")
    return " · ".join(parts)


def _clock(moment: datetime) -> str:
    local = moment.astimezone(IST)
    return f"{local.hour % 12 or 12}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'} IST"


def check(event: object) -> dict:
    """The event in a known shape, or BadEvent."""
    if not isinstance(event, dict):
        raise BadEvent("the event is not an object")
    kind = event.get("event")
    if kind not in KINDS:
        raise BadEvent("unknown event")
    symbol = str(event.get("symbol") or "").strip().upper()
    if not symbol or len(symbol) > 20 or not symbol.replace(".", "").replace("_", "").replace("#", "").isalnum():
        raise BadEvent("bad symbol")
    side = str(event.get("side") or "").strip().upper()
    allowed = ("BUY", "SELL") if kind in ("opened", "modified", "closed") else (
        "BUY LIMIT", "SELL LIMIT", "BUY STOP", "SELL STOP", "BUY STOP LIMIT", "SELL STOP LIMIT")
    if side not in allowed:
        raise BadEvent("bad side")
    clean = {"event": kind, "symbol": symbol, "side": side, "ticket": str(event.get("ticket") or "")[:30]}
    for name in ("price", "sl", "tp", "open_price", "close_price", "points"):
        value = event.get(name)
        if value in (None, ""):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise BadEvent(f"bad {name}") from None
        if number != number or abs(number) > 1e9:
            raise BadEvent(f"bad {name}")
        clean[name] = number
    if kind in ("pending_placed", "pending_cancelled", "opened") and "price" not in clean:
        raise BadEvent("missing price")
    if kind == "closed" and not ("open_price" in clean and "close_price" in clean):
        raise BadEvent("missing prices")
    reason = str(event.get("reason") or "").strip().lower()
    clean["reason"] = reason if reason in ("tp", "sl", "manual", "stopout", "expert", "partial", "") else ""
    return clean


def build_message(event: dict, now: datetime | None = None) -> str:
    """One trade event as a short post. Prices and points only."""
    e = check(event)
    when = _clock(now or datetime.now(timezone.utc))
    symbol, side = e["symbol"], e["side"]
    if e["event"] == "pending_placed":
        body = [f"⏳ *PENDING ORDER* · {symbol}", f"{side} @ {_price(e['price'])}", _levels(e), f"🕒 {when}"]
    elif e["event"] == "pending_cancelled":
        body = [f"🚫 *ORDER CANCELLED* · {symbol}", f"{side} @ {_price(e['price'])} has been cancelled.", f"🕒 {when}"]
    elif e["event"] == "opened":
        icon = "🟢" if side == "BUY" else "🔴"
        body = [f"{icon} *{side} {symbol}* @ {_price(e['price'])}", _levels(e), f"🕒 {when}"]
    elif e["event"] == "modified":
        note = ""
        if e.get("sl") and e.get("open_price") and abs(e["sl"] - e["open_price"]) < 1e-9:
            note = " (breakeven)"
        body = [f"✏️ *{symbol} {side} · update*", _levels(e) + note, f"🕒 {when}"]
    else:
        direction = 1 if side == "BUY" else -1
        move = (e["close_price"] - e["open_price"]) * direction
        title = {"tp": "✅ *TP HIT*", "sl": "❌ *SL HIT*", "manual": "✋ *CLOSED MANUALLY*", "stopout": "⚠️ *STOPPED OUT*",
                 "partial": "➗ *PARTIAL CLOSE*"}.get(
            e["reason"], "🏁 *TRADE CLOSED*")
        points = e.get("points")
        result = f"{'+' if move >= 0 else '-'}{abs(move):,.2f}" + (f" ({points:+,.0f} points)" if points is not None else "")
        body = [f"{title} · {symbol} {side}", f"Entry {_price(e['open_price'])} → Exit {_price(e['close_price'])}",
                f"Result: {result}", f"🕒 {when}"]
    return "\n".join(body + ["", DISCLAIMER])


# -- sending -------------------------------------------------------------------------------

def _post_json(url: str, payload: dict, headers: dict, secrets: tuple[str, ...]) -> tuple[bool, str]:
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": USER_AGENT, **headers})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return 200 <= response.status < 300, f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200]
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        detail = type(exc).__name__
    for secret in secrets:
        if secret:
            detail = detail.replace(secret, "***")
    return False, detail


def send_telegram(text: str, env) -> tuple[bool, str]:
    token, chat = env.get("TELEGRAM_BOT_TOKEN", ""), env.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        return False, "not configured"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    ok, detail = _post_json(url, {"chat_id": chat, "text": text, "parse_mode": "Markdown"}, {}, (token,))
    if not ok and "parse" in detail.lower():
        ok, detail = _post_json(url, {"chat_id": chat, "text": text}, {}, (token,))
    return ok, detail


def send_whatsapp(text: str, env) -> tuple[bool, str]:
    token, chat = env.get("WHAPI_TOKEN", ""), env.get("WHATSAPP_CHAT_ID", "")
    if not token or not chat:
        return False, "not configured"
    base = (env.get("WHAPI_BASE_URL") or "https://gate.whapi.cloud").rstrip("/")
    return _post_json(f"{base}/messages/text", {"to": chat, "body": text, "no_link_preview": True},
                      {"Authorization": f"Bearer {token}"}, (token,))


def handle(body: bytes, secret_header: str, env, *, now: datetime | None = None, senders=None) -> tuple[int, dict]:
    """(HTTP status, JSON reply) for one request."""
    expected = env.get("TRADE_WEBHOOK_SECRET", "")
    if len(expected) < 16:
        return 503, {"ok": False, "error": "the receiver is not configured"}
    if not hmac.compare_digest(str(secret_header or ""), expected):
        return 401, {"ok": False, "error": "wrong secret"}
    if len(body) > MAX_BODY:
        return 413, {"ok": False, "error": "too large"}
    try:
        text = build_message(json.loads(body.decode("utf-8")), now)
    except (ValueError, UnicodeDecodeError) as exc:
        return 400, {"ok": False, "error": str(exc)[:100]}
    telegram, whatsapp = senders or (send_telegram, send_whatsapp)
    results = {"telegram": telegram(text, env), "whatsapp": whatsapp(text, env)}
    ok = any(sent for sent, _ in results.values())
    secrets = [env.get(k, "") for k in ("TELEGRAM_BOT_TOKEN", "WHAPI_TOKEN", "TRADE_WEBHOOK_SECRET")]

    def scrub(text: str) -> str:
        for secret in secrets:
            if secret:
                text = text.replace(secret, "***")
        return text
    return (200 if ok else 502), {"ok": ok, **{name: {"sent": sent, "detail": scrub(detail)} for name, (sent, detail) in results.items()}}


class handler(BaseHTTPRequestHandler):  # Vercel's Python runtime looks for this name
    def _reply(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._reply(200, {"ok": True, "service": "trade alerts"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(min(length, MAX_BODY + 1))
        status, payload = handle(body, self.headers.get("X-Trade-Secret", ""), os.environ)
        self._reply(status, payload)
