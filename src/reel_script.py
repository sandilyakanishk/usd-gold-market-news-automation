"""The owner's daily reel script: a Hinglish script about one day's USD news and gold, on a private web page.

    python -m src.main --owner-reel-script [--dry-run] [--date YYYY-MM-DD]

After the day's recap (23:35 India time) the bot writes a short reel script
about that day: the USD releases with their actual figures, how gold moved,
and what comes next. Google Gemini writes it in Hinglish; if the AI is not
available, a plain template version is used instead, so there is always a
script.

The script goes to one place only: the owner's page on Vercel. It is never
posted to Telegram or WhatsApp. The page holds only the latest script, so the
previous day's disappears when the new one arrives. The page opens without a
password and is kept out of search engines; anyone given its address can
read it. Setting REEL_PAGE_PASSWORD instead encrypts the script (AES-GCM, key
derived with PBKDF2) so that the page asks for that password.

Like every card, the script reports news and numbers. It never tells anyone
to buy or sell, and never says where the price will go.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import secrets
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from . import cards, content_cards
from .actuals.service import load_enriched
from .config import PROJECT_ROOT, Settings
from .database.base import EventRepository
from .delivery.models import FAILED, SENT, DeliveryRecord

log = logging.getLogger(__name__)

MESSAGE_TYPE = "OWNER_REEL_SCRIPT"
PROVIDER = "vercel"
DESTINATION = "owner_reel_page"
# The day's script is written once its recap is out; until then the page keeps the previous day's.
READY_AT = time(23, 35)
PAGE_DIR = PROJECT_ROOT / "web" / "reel-page"
VERCEL_API = "https://api.vercel.com"
PBKDF2_ROUNDS = 310_000
STAMP = "%Y-%m-%dT%H:%M:%SZ"

# Reports news; never advises. Explaining what a release is remains fine.
_FORBIDDEN = re.compile(
    r"\b(buy now|sell now|buy karo|sell karo|kharid(o|lo)|bech(o|do)|entry le|entry lo|target \d+|stop ?loss \d+"
    r"|will (rise|fall|go up|go down|hit|reach)|jayega upar|jayega neeche|zaroor (badhega|girega)|guarantee|sure ?shot"
    r"|pakka profit|100 ?%)\b", re.I)


class ReelScriptError(Exception):
    """The script or the page could not be produced. Messages never contain a secret."""


@dataclass(frozen=True)
class ReelScript:
    day: str
    title: str
    hook: str
    lines: list[str]
    on_screen: list[str]
    caption: str
    source: str  # "ai" or "template"

    def to_dict(self) -> dict:
        return {"day": self.day, "title": self.title, "hook": self.hook, "lines": self.lines, "on_screen": self.on_screen,
                "caption": self.caption, "source": self.source}


# -- which day ------------------------------------------------------------------------------

def target_day(now: datetime, tz: ZoneInfo | timezone) -> date:
    """Today once today's recap is out, otherwise yesterday."""
    local = now.astimezone(tz)
    return local.date() if local.time() >= READY_AT else local.date() - timedelta(days=1)


# -- the day's facts ------------------------------------------------------------------------

def day_facts(db: EventRepository, day: date, tz: ZoneInfo | timezone, *, now: datetime) -> dict:
    """Everything the script may say, from stored data only."""
    # Every USD event Forex Factory listed for the day, most important first. A quiet day's minor releases are
    # still the day's news, so none is left out.
    items = load_enriched(db, now=now, currency="USD", date_from=day.isoformat(), date_to=day.isoformat())
    rank = {"High": 0, "Medium": 1, "Low": 2}
    releases = []
    for item in sorted(items, key=lambda i: (rank.get(i.event.impact, 3), i.event.time or "", i.event.event_name)):
        e, c = item.event, item.classification
        releases.append({
            "name": e.event_name, "time_ist": _clock(e.time), "impact": e.impact, "forecast": e.forecast,
            "previous": e.previous, "actual": e.actual, "result": item.record.surprise_status if e.actual else None,
            "category": c.category if c else None, "why_it_matters": c.gold_relevance_reason if c else None,
        })
    start, end = cards.day_bounds(day, tz)
    summary = cards.summarize(db.price_snapshots_between(cards.SYMBOL, start, end))
    if summary is None:
        stored = db.latest_daily_price(cards.SYMBOL, (day + timedelta(days=1)).isoformat())
        if stored is not None and stored["day"] == day.isoformat():
            summary = stored
    gold = None
    if summary is not None:
        change = summary["close"] - summary["open"]
        gold = {"open": round(summary["open"], 2), "high": round(summary["high"], 2), "low": round(summary["low"], 2),
                "close": round(summary["close"], 2), "change": round(change, 2),
                "change_percent": round(change / summary["open"] * 100, 2) if summary["open"] else 0.0}
    ahead = []
    for event in cards.week_ahead_events(db, day)[:6]:
        ahead.append({"name": event.event_name, "date": event.date, "time": event.time, "impact": event.impact})
    return {"day": day.isoformat(), "weekday": day.strftime("%A"), "releases": releases, "gold": gold, "coming_up": ahead}


# -- writing the script ---------------------------------------------------------------------

def build_prompt(facts: dict) -> str:
    return "\n".join([
        "You write a short Instagram/YouTube reel script for a gold (XAU/USD) and forex trading creator in India.",
        "Write it in natural Hinglish: Hindi words in Roman script mixed with English, the way young Indian traders speak.",
        "It must be spoken in about 45 to 60 seconds. Build it around the day's US economic news from the Forex Factory",
        "calendar (the 'releases' list): hook on the most important release, then go through the releases, saying what each",
        "one measures and its forecast and previous figures. Only if 'actual' is given may you say what the figure came in at;",
        "otherwise do not claim any result. After the news, one or two lines on how gold moved that day (the 'gold' figures),",
        "then one line about what is coming next ('coming_up'), then a call to follow the page.",
        "Use ONLY the facts below; never invent numbers, times, results or events.",
        "Strict rules: this is news, not advice. Never tell viewers to buy or sell, never give entries, targets or stop-losses,",
        "never say where gold or the dollar will go. Explaining why a figure matters for the dollar or gold is fine.",
        "No hashtags inside the spoken lines. No emojis in the spoken lines.",
        'Reply with JSON only: {"title": "...", "hook": "...", "lines": ["...", "..."], "on_screen": ["...", "..."], "caption": "..."}',
        "title: under 60 characters. hook: one spoken sentence. lines: 5 to 9 short spoken lines after the hook.",
        "on_screen: 3 to 6 very short text overlays. caption: under 300 characters, may end with up to 5 hashtags.",
        "Facts (JSON): " + json.dumps(facts, ensure_ascii=False),
    ])


def _text(value, name: str, limit: int, minimum: int = 3) -> str:
    if not isinstance(value, str):
        raise ReelScriptError(f"{name} is not text")
    cleaned = " ".join(value.split())
    if not (minimum <= len(cleaned) <= limit):
        raise ReelScriptError(f"{name} has {len(cleaned)} characters (allowed {minimum} to {limit})")
    if _FORBIDDEN.search(cleaned):
        raise ReelScriptError(f"{name} reads like trading advice or a prediction")
    return cleaned


def validate(raw: object, day: str, source: str) -> ReelScript:
    if not isinstance(raw, dict):
        raise ReelScriptError("the script is not an object")
    lines, overlays = raw.get("lines"), raw.get("on_screen")
    if not isinstance(lines, list) or not (4 <= len(lines) <= 10):
        raise ReelScriptError("the script needs 4 to 10 spoken lines")
    if not isinstance(overlays, list) or not (2 <= len(overlays) <= 7):
        raise ReelScriptError("the script needs 2 to 7 on-screen texts")
    return ReelScript(
        day=day, title=_text(raw.get("title"), "the title", 80), hook=_text(raw.get("hook"), "the hook", 220, 10),
        lines=[_text(line, "a line", 260, 5) for line in lines], on_screen=[_text(o, "an on-screen text", 70, 2) for o in overlays],
        caption=_text(raw.get("caption"), "the caption", 400, 10), source=source)


def _clock(hhmm: str | None) -> str:
    if not hhmm:
        return "din bhar"
    hour, minute = (int(part) for part in hhmm.split(":")[:2])
    return f"{hour % 12 or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'}"


def template_script(facts: dict) -> ReelScript:
    """The plain version, used when the AI cannot write one. Facts only, in simple Hinglish."""
    day = date.fromisoformat(facts["day"])
    label = f"{day.day} {day.strftime('%B')}"
    gold, releases, ahead = facts.get("gold"), facts.get("releases") or [], facts.get("coming_up") or []
    lines = []
    released = [r for r in releases if r.get("actual")]
    for r in releases[:4]:
        if r.get("actual"):
            versus = f", forecast tha {r['forecast']}" if r.get("forecast") else ""
            lines.append(f"{r['name']} aaya {r['actual']}{versus}.")
        else:
            figures = [f"forecast {r['forecast']}" if r.get("forecast") else "", f"previous {r['previous']}" if r.get("previous") else ""]
            figures = ", ".join(f for f in figures if f)
            lines.append(f"{r['time_ist']} IST par {r['name']} ({r['impact'].lower()} impact)" + (f": {figures}." if figures else "."))
    if not releases:
        lines.append("Forex Factory par aaj US ka koi data release nahi tha.")
    if gold:
        direction = "upar" if gold["change"] > 0 else "neeche" if gold["change"] < 0 else "flat"
        lines.append(f"Gold ne din ki shuruaat ki {gold['open']:,.2f} dollar par.")
        lines.append(f"High bana {gold['high']:,.2f} aur low {gold['low']:,.2f}.")
        lines.append(f"Din khatam hua {gold['close']:,.2f} par, yaani {abs(gold['change']):,.2f} dollar {direction}.")
    if ahead:
        nxt = ahead[0]
        lines.append(f"Aage dhyaan rakho: {nxt['name']}, {_clock(nxt.get('time'))} IST.")
    lines.append("Aise hi daily gold aur dollar updates ke liye page ko follow karo.")
    while len(lines) < 4:
        lines.insert(-1, "Market mein volatility news ke time sabse zyada hoti hai, isliye calendar check karte raho.")
    overlays = [f"USD NEWS · {label}"]
    if gold:
        overlays.append(f"Gold: {gold['close']:,.2f} ({gold['change']:+,.2f})")
    if released:
        overlays.append(f"{released[0]['name']}: {released[0]['actual']}")
    elif releases:
        overlays.append(f"{len(releases)} US releases")
    overlays.append("Follow for daily updates")
    return validate({
        "title": f"Gold aur USD news · {label}",
        "hook": f"{label} ko gold aur dollar mein kya hua? 50 second mein samjho.",
        "lines": lines[:10], "on_screen": overlays[:7],
        "caption": f"{label} ka USD news aur gold recap. Sirf information, trading advice nahi. #gold #xauusd #forex #usd",
    }, facts["day"], "template")


def write_script(facts: dict, settings: Settings, *, ask=content_cards.ask_gemini) -> ReelScript:
    """The AI's script if it is fit to use, otherwise the template version."""
    if getattr(settings, "gemini_api_key", None):
        try:
            raw = content_cards.ask_any_model(build_prompt(facts), settings, ask=ask)
            return validate(raw, facts["day"], "ai")
        except (content_cards.ContentError, ReelScriptError) as exc:
            log.warning("Reel script: the AI's version was not used (%s); using the template.", exc)
    return template_script(facts)


# -- the private page -----------------------------------------------------------------------

def encrypt(payload: dict, password: str) -> dict:
    """AES-256-GCM with a key derived from the password (PBKDF2-SHA256). The page decrypts it in the browser."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    if not password or len(password) < 8:
        raise ReelScriptError("REEL_PAGE_PASSWORD must have at least 8 characters.")
    salt, iv = secrets.token_bytes(16), secrets.token_bytes(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=PBKDF2_ROUNDS).derive(password.encode("utf-8"))
    sealed = AESGCM(key).encrypt(iv, json.dumps(payload, ensure_ascii=False).encode("utf-8"), None)
    b64 = lambda raw: base64.b64encode(raw).decode("ascii")
    return {"v": 1, "kdf": "PBKDF2-SHA256", "iterations": PBKDF2_ROUNDS, "salt": b64(salt), "iv": b64(iv), "data": b64(sealed)}


def decrypt(sealed: dict, password: str) -> dict:
    """The reverse of encrypt (used by the tests; the page does the same in JavaScript)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    raw = lambda name: base64.b64decode(sealed[name])
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=raw("salt"), iterations=int(sealed["iterations"])).derive(password.encode("utf-8"))
    return json.loads(AESGCM(key).decrypt(raw("iv"), raw("data"), None))


def site_files(script: ReelScript, password: str | None, generated_at: datetime) -> dict[str, str]:
    """The page and its one data file: the script as it is, or encrypted when a password is set."""
    payload = {**script.to_dict(), "generated_at": generated_at.astimezone(timezone.utc).strftime(STAMP)}
    data = encrypt(payload, password) if password else {"v": 0, "plain": payload}
    return {
        "index.html": (PAGE_DIR / "index.html").read_text(encoding="utf-8"),
        "script.json": json.dumps(data, ensure_ascii=False),
        "robots.txt": "User-agent: *\nDisallow: /\n",
    }


def _vercel(method: str, path: str, token: str, payload: dict | None = None, *, timeout: int = 30) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(VERCEL_API + path, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": "usd-gold-calendar-collector/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8", errors="replace")).get("error", {}).get("message", "")
        except (ValueError, AttributeError, OSError):
            detail = ""
        raise ReelScriptError(f"Vercel answered HTTP {exc.code}. {str(detail).replace(token, '***')[:200]}".strip()) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ReelScriptError(f"Vercel could not be reached: {type(exc).__name__}.") from None
    return json.loads(body) if body.strip() else {}


def publish(files: dict[str, str], token: str, project: str, *, call=_vercel) -> tuple[str, int]:
    """Deploy the page to production, then delete the project's older deployments. Returns (address, deleted)."""
    deployment = call("POST", "/v13/deployments", token, {
        "name": project, "target": "production",
        "files": [{"file": name, "data": content} for name, content in files.items()],
        "projectSettings": {"framework": None, "buildCommand": None, "outputDirectory": None, "installCommand": None},
    })
    new_id = deployment.get("id")
    if not new_id:
        raise ReelScriptError("Vercel did not confirm the deployment.")
    project_id = deployment.get("projectId")
    deleted = 0
    if project_id:
        listing = call("GET", f"/v6/deployments?projectId={project_id}&limit=50", token)
        for old in listing.get("deployments") or []:
            old_id = old.get("uid") or old.get("id")
            if old_id and old_id != new_id:
                try:
                    call("DELETE", f"/v13/deployments/{old_id}", token)
                    deleted += 1
                except ReelScriptError as exc:  # an old copy left behind is still encrypted
                    log.warning("Reel page: an older deployment could not be deleted (%s).", exc)
    return f"https://{project}.vercel.app", deleted


# -- one run --------------------------------------------------------------------------------

def run(db: EventRepository, settings: Settings, *, now: datetime | None = None, day: date | None = None, dry_run: bool = False,
        environ=None, ask=content_cards.ask_gemini, call=_vercel) -> tuple[str, ReelScript | None, str]:
    """Write and publish the script for `day` (by default the latest finished day) once. Returns (outcome, script, detail)."""
    env = os.environ if environ is None else environ
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    tz = ZoneInfo(settings.display_timezone) if settings.display_timezone else timezone.utc
    day = day or target_day(now, tz)
    key = f"REEL_SCRIPT_{day.isoformat()}"
    existing = db.get_delivery(key, PROVIDER, DESTINATION)
    if existing is not None and existing.status == SENT and not dry_run:
        return "ALREADY_SENT", None, f"the {day.isoformat()} script is already on the page"
    token, password = (env.get("VERCEL_TOKEN") or "").strip(), (env.get("REEL_PAGE_PASSWORD") or "").strip()
    project = (env.get("REEL_PAGE_PROJECT") or "reel-scripts-owner").strip()
    if not dry_run and not token:
        # Checked before asking the AI, so an unconfigured page costs nothing on every run.
        return "NOT_CONFIGURED", None, "VERCEL_TOKEN is not set"
    script = write_script(day_facts(db, day, tz, now=now), settings, ask=ask)
    if dry_run:
        return "DRY_RUN", script, "nothing was published"
    stamp = now.strftime(STAMP)
    record = DeliveryRecord(message_key=key, provider=PROVIDER, destination_id=DESTINATION, destination=DESTINATION,
                            message_type=MESSAGE_TYPE, status=FAILED, attempts=(existing.attempts + 1) if existing else 1,
                            created_at=existing.created_at if existing else stamp, updated_at=stamp)
    try:
        address, deleted = publish(site_files(script, password or None, now), token, project, call=call)
    except ReelScriptError as exc:
        record.error = str(exc)
        db.save_delivery(record)
        return "FAILED", script, str(exc)
    record.status, record.sent_at, record.provider_message_id = SENT, stamp, address
    db.save_delivery(record)
    return "SENT", script, f"{address} (older versions removed: {deleted})"


def render_text(script: ReelScript) -> str:
    """The script as plain text, for a dry run."""
    return "\n".join([f"🎬 {script.title}  [{script.day}, {script.source}]", "", f"HOOK: {script.hook}", "",
                      *[f"{i}. {line}" for i, line in enumerate(script.lines, 1)], "",
                      "ON SCREEN: " + " | ".join(script.on_screen), "", f"CAPTION: {script.caption}"])


__all__ = ["ReelScript", "ReelScriptError", "build_prompt", "day_facts", "decrypt", "encrypt", "publish", "render_text", "run",
           "site_files", "target_day", "template_script", "validate", "write_script"]
