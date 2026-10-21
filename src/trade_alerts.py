"""Deploys the trade alerts receiver (web/trade-alerts) to Vercel:  python -m src.main --deploy-trade-alerts

Two ways in, one format out (Telegram channel and WhatsApp Channel):
  api/trade.py      events from the MT5 add-on
  api/telegram.py   trades the owner types to the bot in a private chat

This copies the channel settings from .env into Vercel's environment
variables, creates the shared secrets in .env if there are none, finds the
owner's Telegram id from the setup word they sent the bot, deploys, and
points the bot's webhook at api/telegram. No secret is printed.
"""

from __future__ import annotations

import os
import re
import secrets
from pathlib import Path

from . import reel_script
from .config import PROJECT_ROOT, Settings

RECEIVER_DIR = PROJECT_ROOT / "web" / "trade-alerts"
PROJECT = "trade-alerts-owner"


class TradeAlertsError(Exception):
    """The receiver could not be deployed. Messages never contain a secret."""


SETUP_WORD = "owner-setup"


def save_setting(env_file: Path, name: str, value: str, environ=None) -> None:
    env = os.environ if environ is None else environ
    text = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
    if re.search(rf"(?m)^{name}=", text):
        text = re.sub(rf"(?m)^{name}=.*$", f"{name}={value}", text)
    else:
        text = text.rstrip("\n") + f"\n{name}={value}\n"
    env_file.write_text(text, encoding="utf-8")
    env[name] = value


def ensure_secret(env_file: Path, environ=None, name: str = "TRADE_WEBHOOK_SECRET") -> str:
    """A shared secret, created and saved in .env on first use."""
    env = os.environ if environ is None else environ
    secret = (env.get(name) or "").strip()
    if len(secret) >= 16:
        return secret
    secret = secrets.token_urlsafe(24)
    save_setting(env_file, name, secret, env)
    return secret


def find_owner_id(token: str, *, call=None) -> str | None:
    """The Telegram id of whoever sent the bot the setup word in a private chat (the newest such message)."""
    import json as _json
    import urllib.request as _request
    if call is None:
        def call(method, payload):
            request = _request.Request(f"https://api.telegram.org/bot{token}/{method}", data=_json.dumps(payload).encode(),
                                       headers={"Content-Type": "application/json"}, method="POST")
            with _request.urlopen(request, timeout=20) as response:
                return _json.loads(response.read())
    call("deleteWebhook", {})  # updates can only be read while no webhook is set
    updates = call("getUpdates", {"allowed_updates": ["message"]}).get("result") or []
    for update in reversed(updates):
        message = update.get("message") or {}
        if (message.get("chat") or {}).get("type") == "private" and (message.get("text") or "").strip().lower() == SETUP_WORD:
            return str((message.get("from") or {}).get("id") or "") or None
    return None


def set_webhook(token: str, url: str, secret: str, *, call=None) -> None:
    import json as _json
    import urllib.request as _request
    payload = {"url": url, "secret_token": secret, "allowed_updates": ["message"], "drop_pending_updates": True}
    if call is None:
        request = _request.Request(f"https://api.telegram.org/bot{token}/setWebhook", data=_json.dumps(payload).encode(),
                                   headers={"Content-Type": "application/json"}, method="POST")
        with _request.urlopen(request, timeout=20) as response:
            reply = _json.loads(response.read())
    else:
        reply = call("setWebhook", payload)
    if not reply.get("ok"):
        raise TradeAlertsError("Telegram did not accept the webhook.")


def receiver_files() -> dict[str, str]:
    return {
        "api/trade.py": (RECEIVER_DIR / "api" / "trade.py").read_text(encoding="utf-8"),
        "api/telegram.py": (RECEIVER_DIR / "api" / "telegram.py").read_text(encoding="utf-8"),
        "requirements.txt": (RECEIVER_DIR / "requirements.txt").read_text(encoding="utf-8"),
    }


def deploy(settings: Settings, token: str, secret: str, *, call=reel_script._vercel, project: str = PROJECT,
           extra: dict | None = None) -> str:
    """Create the project if needed, set its environment variables, deploy. Returns the receiver's address."""
    values = {
        "TRADE_WEBHOOK_SECRET": secret,
        "TELEGRAM_BOT_TOKEN": settings.telegram_bot_token or "",
        "TELEGRAM_CHAT_ID": settings.telegram_chat_id or "",
        "WHAPI_TOKEN": settings.whapi_token or "",
        "WHATSAPP_CHAT_ID": settings.whatsapp_announcement_chat_id or "",
        "WHAPI_BASE_URL": settings.whapi_base_url,
        **(extra or {}),
    }
    missing = [name for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not values[name]]
    if missing:
        raise TradeAlertsError("Missing settings: " + ", ".join(missing))
    try:
        found = call("GET", f"/v9/projects/{project}", token)
    except reel_script.ReelScriptError:
        found = call("POST", "/v10/projects", token, {"name": project, "framework": None})
    project_id = found.get("id")
    if not project_id:
        raise TradeAlertsError("Vercel did not return the project.")
    call("POST", f"/v10/projects/{project_id}/env?upsert=true", token,
         [{"key": key, "value": value, "type": "encrypted", "target": ["production"]} for key, value in values.items() if value])
    address, _ = reel_script.publish(receiver_files(), token, project, call=call)
    return address
