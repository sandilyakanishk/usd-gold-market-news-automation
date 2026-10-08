"""Live alert: posts to Telegram when the owner's YouTube channel goes live.

    python -m src.main --telegram-send-live [--dry-run]

Meant to run every minute. Each run asks YouTube's official Data API which of
the channel's newest videos is a live broadcast that is on air right now, and
posts one alert per broadcast: the stream's title, its cover and its link.

The broadcast's video id is the message identity, so a stream is announced
once however many runs see it. The API key travels in a request header, never
in an address, and never appears in a message or a log line.

Cost: two API units per run (2,880 a day of the free 10,000).
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

from . import social
from .config import Settings
from .database.base import EventRepository
from .delivery.models import (
    DESTINATION_TELEGRAM_CHANNEL, OUTCOME_ALREADY_SENT, OUTCOME_DRY_RUN, OUTCOME_FAILED, OUTCOME_SENT,
    PROVIDER_TELEGRAM, DeliveryResult,
)
from .delivery.service import deliver_text
from .delivery.telegram import (
    TelegramAuthError, TelegramClient, TelegramDestinationError, TelegramError, TelegramNetworkError, describe_chat,
)

log = logging.getLogger(__name__)

MESSAGE_TYPE = "LIVE_ALERT"
API_BASE = "https://www.googleapis.com/youtube/v3"
NEWEST = 10  # how many of the channel's newest videos are looked at


class LiveCheckError(Exception):
    """YouTube's API could not be asked. Messages never contain the key."""


@dataclass(frozen=True)
class LiveStream:
    video_id: str
    title: str
    started_at: datetime | None

    @property
    def link(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"

    @property
    def message_key(self) -> str:
        return f"LIVE_YT_{self.video_id}"

    @property
    def images(self) -> list[str]:
        base = f"https://i.ytimg.com/vi/{self.video_id}"
        return [f"{base}/maxresdefault_live.jpg", f"{base}/maxresdefault.jpg", f"{base}/hqdefault_live.jpg", f"{base}/hqdefault.jpg"]


# -- YouTube Data API -----------------------------------------------------------------------

def api_get(resource: str, params: dict, api_key: str, *, timeout: int = 20, user_agent: str = "") -> dict:
    """One GET to the YouTube Data API. The key is sent as a header."""
    request = urllib.request.Request(
        f"{API_BASE}/{resource}?{urllib.parse.urlencode(params)}",
        headers={"X-Goog-Api-Key": api_key, "Accept": "application/json", "User-Agent": user_agent})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise LiveCheckError(f"YouTube's API answered HTTP {exc.code} ({_reason(exc)}).") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LiveCheckError(f"YouTube's API could not be reached: {type(exc).__name__}.") from None
    try:
        parsed = json.loads(body)
    except ValueError:
        raise LiveCheckError("YouTube's API returned a reply that is not JSON.") from None
    if not isinstance(parsed, dict):
        raise LiveCheckError("YouTube's API returned an unexpected reply.")
    return parsed


def _reason(exc: urllib.error.HTTPError) -> str:
    """Google's short reason code (quotaExceeded, keyInvalid, ...), which never contains the key."""
    try:
        errors = json.loads(exc.read().decode("utf-8", errors="replace"))["error"]["errors"]
        reason = str(errors[0]["reason"])
        return reason if reason.isalnum() else "no reason given"
    except (ValueError, KeyError, IndexError, TypeError, OSError, AttributeError):
        return "no reason given"


def uploads_playlist(channel_id: str) -> str:
    """A channel's uploads playlist has the channel's id with UU in place of UC."""
    if not social._CHANNEL_ID.match(channel_id or ""):
        raise LiveCheckError("YOUTUBE_CHANNEL_ID is not a YouTube channel id (it starts with UC and has 24 characters).")
    return "UU" + channel_id[2:]


def newest_video_ids(channel_id: str, api_key: str, *, get=api_get, **options) -> list[str]:
    data = get("playlistItems", {"part": "contentDetails", "playlistId": uploads_playlist(channel_id), "maxResults": NEWEST},
               api_key, **options)
    ids = []
    for item in data.get("items") or []:
        video_id = ((item or {}).get("contentDetails") or {}).get("videoId")
        if isinstance(video_id, str) and social._VIDEO_ID.match(video_id) and video_id not in ids:
            ids.append(video_id)
    return ids


def _instant(value: object) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def parse_live(data: dict) -> list[LiveStream]:
    """The broadcasts in a videos.list reply that are on air right now."""
    streams = []
    for item in data.get("items") or []:
        if not isinstance(item, dict):
            continue
        snippet, details = item.get("snippet") or {}, item.get("liveStreamingDetails") or {}
        video_id = item.get("id")
        if not isinstance(video_id, str) or not social._VIDEO_ID.match(video_id):
            continue
        # "live" means on air; "upcoming" is scheduled and "none" is an ordinary or finished video.
        if snippet.get("liveBroadcastContent") != "live" or details.get("actualEndTime"):
            continue
        streams.append(LiveStream(video_id, str(snippet.get("title") or "").strip(), _instant(details.get("actualStartTime"))))
    return streams


def find_live(channel_id: str, api_key: str, *, extra_ids: list[str] | None = None, get=api_get, **options) -> list[LiveStream]:
    ids = newest_video_ids(channel_id, api_key, get=get, **options)
    for video_id in extra_ids or []:
        if video_id not in ids:
            ids.append(video_id)
    if not ids:
        return []
    data = get("videos", {"part": "snippet,liveStreamingDetails", "id": ",".join(ids[:50])}, api_key, **options)
    return parse_live(data)


# -- text -----------------------------------------------------------------------------------

def build_alert(stream: LiveStream, instagram_url: str | None = None) -> str:
    """The alert. The title is the owner's own, unchanged."""
    lines = ["🔴 WE ARE LIVE NOW!", ""]
    if stream.title:
        lines += [stream.title, ""]
    lines.append(f"▶️ Watch live: {stream.link}")
    if instagram_url:
        lines.append(f"📸 Instagram: {instagram_url}")
    lines += ["", "🔔 The stream has just started. Tap the link to join."]
    text = "\n".join(lines)
    if len(text) > social.CAPTION_LIMIT:  # only an extremely long title could do this
        over = len(text) - social.CAPTION_LIMIT
        text = text.replace(stream.title, stream.title[:len(stream.title) - over - 1].rstrip() + "…")
    return text


class _AlertPost:
    """Posts the alert with the stream's cover if one can be had right now; otherwise as a link post.

    An alert must not wait: unlike a reel, it is worth nothing later.
    """

    def __init__(self, client: TelegramClient, stream: LiveStream, download):
        self._client, self._stream, self._download = client, stream, download

    def send_text(self, chat_id: str, text: str, **_: object) -> str:
        for url in self._stream.images:
            image = self._download(url)
            if image is None:
                continue
            try:
                return self._client.send_photo_bytes(chat_id, image, text)
            except (TelegramAuthError, TelegramDestinationError, TelegramNetworkError):
                raise
            except TelegramError as exc:
                log.warning("Telegram did not accept the stream's cover (%s); trying the next option.", exc)
        return self._client.send_text(chat_id, text, markdown=False, link_preview=True)


# -- sending --------------------------------------------------------------------------------

def send_live_alerts(db: EventRepository, client: TelegramClient | None, settings: Settings, chat_id: str, *,
                     dry_run: bool = False, now: datetime | None = None, get=api_get, download=None,
                     ) -> list[tuple[LiveStream, DeliveryResult, str]]:
    """Announce every broadcast that is on air and not yet announced."""
    if download is None:
        def download(url: str) -> bytes | None:
            return social.download_image(url, timeout=settings.request_timeout_seconds, user_agent=settings.user_agent)
    streams = find_live(settings.youtube_channel_id, settings.youtube_api_key, get=get,
                        timeout=settings.request_timeout_seconds, user_agent=settings.user_agent)
    results = []
    for stream in streams:
        text = build_alert(stream, social.clean_profile_url(settings.instagram_profile_url))
        result = deliver_text(
            db, None if client is None else _AlertPost(client, stream, download),
            message_key=stream.message_key, message_type=MESSAGE_TYPE, text=text, chat_id=chat_id, dry_run=dry_run,
            now=now, provider=PROVIDER_TELEGRAM, destination=DESTINATION_TELEGRAM_CHANNEL, label=describe_chat(chat_id))
        results.append((stream, result, text))
    return results


__all__ = [
    "LiveCheckError", "LiveStream", "MESSAGE_TYPE", "OUTCOME_ALREADY_SENT", "OUTCOME_DRY_RUN", "OUTCOME_FAILED",
    "OUTCOME_SENT", "api_get", "build_alert", "find_live", "newest_video_ids", "parse_live", "send_live_alerts",
    "uploads_playlist",
]
