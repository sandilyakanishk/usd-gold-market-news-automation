"""Runtime settings, read from environment variables (optionally via a .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"


def load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, existing environment wins."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _path(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else PROJECT_ROOT / p


def _retention_days(value: str) -> int:
    try:
        days = int(value)
    except ValueError:
        days = 0
    if days < 1:
        raise ValueError(f"CALENDAR_RETENTION_DAYS must be a whole number of days, 1 or more (got {value!r}).")
    return days


DEFAULT_GOLD_PRICE_URL = "https://api.gold-api.com/price/XAU"
# The day video forwarding was switched on.
DEFAULT_VIDEO_POSTS_SINCE = "2026-10-08T00:00:00Z"


def _youtube_forward(value: str) -> str:
    value = value.strip().lower()
    if value not in ("shorts", "all"):
        raise ValueError("YOUTUBE_FORWARD must be shorts or all.")
    return value


@dataclass(frozen=True)
class Settings:
    feed_url: str
    display_timezone: str | None
    database_path: Path
    raw_cache_path: Path
    gold_config_path: Path
    log_path: Path
    min_fetch_interval_seconds: int
    request_timeout_seconds: int
    user_agent: str
    log_level: str
    database_backend: str = "sqlite"  # "sqlite" (local) or "postgres" (production)
    database_url: str | None = None  # PostgreSQL connection string; secret, never logged
    calendar_retention_days: int = 14
    db_connect_timeout_seconds: int = 10
    priority_rules_path: Path = PROJECT_ROOT / "config" / "gold_priority_rules.json"
    actual_mapping_path: Path = PROJECT_ROOT / "config" / "actual_event_mapping.json"
    bls_api_key: str | None = None  # optional; secret, never logged
    fred_api_key: str | None = None  # needed for FRED-sourced events; secret, never logged
    actuals_retry_days: int = 7
    message_templates_path: Path = PROJECT_ROOT / "config" / "message_templates.json"
    headline_rules_path: Path = PROJECT_ROOT / "config" / "headline_rules.json"
    preview_fixture_path: Path = PROJECT_ROOT / "fixtures" / "message_preview_events.json"
    whapi_token: str | None = None  # secret, never logged
    whapi_base_url: str = "https://gate.whapi.cloud"
    whatsapp_community_id: str | None = None
    whatsapp_announcement_chat_id: str | None = None
    telegram_bot_token: str | None = None  # secret, never logged
    telegram_chat_id: str | None = None  # "@channelname" or a numeric id
    gold_price_url: str = DEFAULT_GOLD_PRICE_URL  # free XAU/USD quote for the market pulse; no key
    youtube_channel_id: str | None = None  # the owner's channel (UC...); its new Shorts are forwarded to Telegram
    youtube_forward: str = "shorts"  # "shorts" or "all" (also long videos and live streams)
    video_posts_since: str = DEFAULT_VIDEO_POSTS_SINCE  # older uploads are never forwarded
    instagram_profile_url: str | None = None  # shown under every forwarded video

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(PROJECT_ROOT / ".env")
        env = os.environ.get
        return cls(
            feed_url=env("FF_CALENDAR_URL", DEFAULT_FEED_URL),
            # Empty string => keep the UTC offset the feed itself provides.
            display_timezone=env("DISPLAY_TIMEZONE", "Asia/Kolkata") or None,
            database_path=_path(env("SQLITE_PATH") or env("DATABASE_PATH") or "data/events.db"),
            raw_cache_path=_path(env("RAW_CACHE_PATH", "data/raw/ff_calendar_thisweek.json")),
            gold_config_path=_path(env("GOLD_RELEVANCE_CONFIG", "config/gold_relevance.json")),
            log_path=_path(env("LOG_PATH", "logs/collector.log")),
            min_fetch_interval_seconds=int(env("MIN_FETCH_INTERVAL_SECONDS", "1800")),
            request_timeout_seconds=int(env("REQUEST_TIMEOUT_SECONDS", "20")),
            user_agent=env("HTTP_USER_AGENT", "usd-gold-calendar-collector/0.1 (personal, low-frequency)"),
            log_level=env("LOG_LEVEL", "INFO"),
            database_backend=(env("DATABASE_BACKEND") or "sqlite").strip().lower(),
            database_url=env("DATABASE_URL") or None,
            calendar_retention_days=_retention_days(env("CALENDAR_RETENTION_DAYS") or "14"),
            db_connect_timeout_seconds=int(env("DB_CONNECT_TIMEOUT_SECONDS") or "10"),
            priority_rules_path=_path(env("GOLD_PRIORITY_RULES") or "config/gold_priority_rules.json"),
            actual_mapping_path=_path(env("ACTUAL_EVENT_MAPPING") or "config/actual_event_mapping.json"),
            bls_api_key=env("BLS_API_KEY") or None,
            fred_api_key=env("FRED_API_KEY") or None,
            actuals_retry_days=int(env("ACTUALS_RETRY_DAYS") or "7"),
            message_templates_path=_path(env("MESSAGE_TEMPLATES") or "config/message_templates.json"),
            headline_rules_path=_path(env("HEADLINE_RULES") or "config/headline_rules.json"),
            preview_fixture_path=_path(env("PREVIEW_FIXTURE") or "fixtures/message_preview_events.json"),
            whapi_token=env("WHAPI_TOKEN") or None,
            whapi_base_url=env("WHAPI_BASE_URL") or "https://gate.whapi.cloud",
            whatsapp_community_id=env("WHATSAPP_COMMUNITY_ID") or None,
            whatsapp_announcement_chat_id=env("WHATSAPP_ANNOUNCEMENT_CHAT_ID") or None,
            telegram_bot_token=env("TELEGRAM_BOT_TOKEN") or None,
            telegram_chat_id=env("TELEGRAM_CHAT_ID") or None,
            gold_price_url=env("GOLD_PRICE_URL") or DEFAULT_GOLD_PRICE_URL,
            youtube_channel_id=(env("YOUTUBE_CHANNEL_ID") or "").strip() or None,
            youtube_forward=_youtube_forward(env("YOUTUBE_FORWARD") or "shorts"),
            video_posts_since=env("VIDEO_POSTS_SINCE") or DEFAULT_VIDEO_POSTS_SINCE,
            instagram_profile_url=(env("INSTAGRAM_PROFILE_URL") or "").strip() or None,
        )
