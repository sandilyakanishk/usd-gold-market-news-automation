"""Deploys the trade alerts receiver (web/trade-alerts) to Vercel:  python -m src.main --deploy-trade-alerts

The receiver posts each MT5 trade event to Telegram and WhatsApp. It needs
the channel settings as Vercel environment variables; this copies them from
the project's settings (.env) through Vercel's API, creates the shared
TRADE_WEBHOOK_SECRET in .env if there is none, and deploys the function. No
secret is printed.
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


def ensure_secret(env_file: Path, environ=None) -> str:
    """The shared secret between MT5 and the receiver; created and saved in .env on first use."""
    env = os.environ if environ is None else environ
    secret = (env.get("TRADE_WEBHOOK_SECRET") or "").strip()
    if len(secret) >= 16:
        return secret
    secret = secrets.token_urlsafe(24)
    text = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
    if re.search(r"(?m)^TRADE_WEBHOOK_SECRET=", text):
        text = re.sub(r"(?m)^TRADE_WEBHOOK_SECRET=.*$", f"TRADE_WEBHOOK_SECRET={secret}", text)
    else:
        text = text.rstrip("\n") + f"\nTRADE_WEBHOOK_SECRET={secret}\n"
    env_file.write_text(text, encoding="utf-8")
    env["TRADE_WEBHOOK_SECRET"] = secret
    return secret


def receiver_files() -> dict[str, str]:
    return {
        "api/trade.py": (RECEIVER_DIR / "api" / "trade.py").read_text(encoding="utf-8"),
        "requirements.txt": (RECEIVER_DIR / "requirements.txt").read_text(encoding="utf-8"),
    }


def deploy(settings: Settings, token: str, secret: str, *, call=reel_script._vercel, project: str = PROJECT) -> str:
    """Create the project if needed, set its environment variables, deploy. Returns the receiver's address."""
    values = {
        "TRADE_WEBHOOK_SECRET": secret,
        "TELEGRAM_BOT_TOKEN": settings.telegram_bot_token or "",
        "TELEGRAM_CHAT_ID": settings.telegram_chat_id or "",
        "WHAPI_TOKEN": settings.whapi_token or "",
        "WHATSAPP_CHAT_ID": settings.whatsapp_announcement_chat_id or "",
        "WHAPI_BASE_URL": settings.whapi_base_url,
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
