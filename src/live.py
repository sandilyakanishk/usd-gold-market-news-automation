"""Live alert: posts to Telegram when the owner's YouTube channel goes live.

    python -m src.main --telegram-send-live [--dry-run]

Meant to run every minute. Each run finds out whether a broadcast is on air
right now and posts one alert per broadcast: the stream's title, its cover
and its link. The broadcast's video id is the message identity, so a stream
is announced once however many runs see it.

Two ways of finding out, chosen by configuration:

  no key (default)   the channel's public page youtube.com/channel/<id>/live,
                     and, because that page does not show every stream, the
                     own pages of the channel's newest videos from its feed.
                     Nothing to set up, but it reads web pages, so a change
                     on YouTube's side can break it.
  YOUTUBE_API_KEY    YouTube's official Data API (two units per run). The
                     key travels in a request header, never in an address,
                     and never appears in a message or a log line.
"""

from __future__ import annotations

import html
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

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


# -- the public page (no key) -----------------------------------------------------------------

LIVE_PAGE = "https://www.youtube.com/channel/{channel_id}/live"
WATCH_PAGE = "https://www.youtube.com/watch?v={video_id}"
RECENT_STREAM_AGE = timedelta(hours=24)
MAX_CANDIDATES = 3  # at most this many video pages are read per check
MAX_PAGE_BYTES = 5 * 1024 * 1024
_CANONICAL = re.compile(r'<link rel="canonical" href="https://www\.youtube\.com/(watch\?v=([A-Za-z0-9_-]{6,20})|channel/[A-Za-z0-9_-]+)"')
_TITLE = re.compile(r'<meta name="title" content="([^"]*)"')


def parse_live_page(page: str) -> list[LiveStream]:
    """The broadcast shown on a channel's /live page, if it is on air.

    The page is the channel itself when nothing is on, and a watch page when a
    broadcast exists. A watch page for a stream that is only scheduled says
    so ("isUpcoming"); one on air carries "isLive":true.
    """
    canonical = _CANONICAL.search(page)
    if canonical is None:
        # Not a page this code understands (a consent or error page, or a redesign): say so rather than guess.
        raise LiveCheckError("YouTube's live page did not look as expected.")
    video_id = canonical.group(2)
    if not video_id:
        return []  # the channel page: nothing is on
    on_air = '"isLive":true' in page and '"isUpcoming":true' not in page and '"status":"LIVE_STREAM_OFFLINE"' not in page
    if not on_air:
        return []
    title = _TITLE.search(page)
    return [LiveStream(video_id, html.unescape(title.group(1)).strip() if title else "", None)]


def _get_page(url: str, *, timeout: int, user_agent: str, what: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept-Language": "en-US,en"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(MAX_PAGE_BYTES).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise LiveCheckError(f"YouTube's {what} answered HTTP {exc.code}.") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LiveCheckError(f"YouTube's {what} could not be reached: {type(exc).__name__}.") from None


def parse_watch_page(page: str, video_id: str) -> LiveStream | None:
    """The broadcast a video's own page describes, if it is on air right now.

    A stream that has been set up but has not started says "isUpcoming" and
    LIVE_STREAM_OFFLINE; a finished one no longer says "isLive".
    """
    if '"isLiveNow":true' in page:
        on_air = True
    elif '"isLiveNow":false' in page:
        on_air = False
    else:
        on_air = '"isLive":true' in page and '"isUpcoming":true' not in page and '"status":"LIVE_STREAM_OFFLINE"' not in page
    if not on_air:
        return None
    title = _TITLE.search(page)
    return LiveStream(video_id, html.unescape(title.group(1)).strip() if title else "", None)


def recent_stream_ids(channel_id: str, *, now: datetime | None = None, fetch_feed=None, **options) -> list[str]:
    """Videos from the channel's feed that could be a broadcast on air: the newest ordinary video
    (a stream is listed as one) and any others published within the last day. Shorts are never streams."""
    fetch_feed = fetch_feed or social.fetch_feed
    now = now or datetime.now(timezone.utc)
    videos = sorted((v for v in fetch_feed(channel_id, **options) if not v.is_short), key=lambda v: v.published, reverse=True)
    recent = [v.video_id for v in videos if now - v.published <= RECENT_STREAM_AGE]
    newest = [videos[0].video_id] if videos else []
    return list(dict.fromkeys(newest + recent))[:MAX_CANDIDATES]


def find_live_on_page(channel_id: str, *, timeout: int = 20, user_agent: str = "", now: datetime | None = None,
                      get_page=_get_page, fetch_feed=None) -> list[LiveStream]:
    """Find a broadcast on air without an API key.

    Two looks, because YouTube's /live page does not show every stream:
      1. the channel's /live page, which is the stream's own page when it does;
      2. the channel's feed, whose newest ordinary videos are each checked on their own page.
    One of the two failing is tolerated; both failing is an error.
    """
    if not social._CHANNEL_ID.match(channel_id or ""):
        raise LiveCheckError("YOUTUBE_CHANNEL_ID is not a YouTube channel id (it starts with UC and has 24 characters).")
    options = dict(timeout=timeout, user_agent=user_agent)
    problems, streams = [], []
    try:
        streams = parse_live_page(get_page(LIVE_PAGE.format(channel_id=channel_id), what="live page", **options))
    except LiveCheckError as exc:
        problems.append(str(exc))
    if streams:
        return streams
    try:
        candidates = recent_stream_ids(channel_id, now=now, fetch_feed=fetch_feed, **options)
    except social.VideoFeedError as exc:
        problems.append(str(exc))
        candidates = None
    if candidates is None and len(problems) == 2:
        raise LiveCheckError(" ".join(problems))
    for video_id in candidates or []:
        try:
            stream = parse_watch_page(get_page(WATCH_PAGE.format(video_id=video_id), what="video page", **options), video_id)
        except LiveCheckError as exc:
            log.warning("Live check: %s", exc)
            continue
        if stream is not None:
            streams.append(stream)
    return streams


def stream_checker(settings: Settings, *, get=api_get):
    """For video forwarding: a function telling whether a video is a stream, using the official API.

    Only available with an API key; returns None without one. The function
    itself returns None when the API cannot answer, so nothing is posted on a guess.
    """
    if not settings.youtube_api_key:
        return None

    def check(video) -> bool | None:
        try:
            data = get("videos", {"part": "snippet,liveStreamingDetails", "id": video.video_id}, settings.youtube_api_key,
                       timeout=settings.request_timeout_seconds, user_agent=settings.user_agent)
        except LiveCheckError as exc:
            log.warning("Video check: %s", exc)
            return None
        for item in data.get("items") or []:
            if isinstance(item, dict) and item.get("id") == video.video_id:
                broadcast = (item.get("snippet") or {}).get("liveBroadcastContent")
                return bool(item.get("liveStreamingDetails")) or broadcast in ("live", "upcoming")
        return None
    return check


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
                     read_page=find_live_on_page) -> list[tuple[LiveStream, DeliveryResult, str]]:
    """Announce every broadcast that is on air and not yet announced."""
    if download is None:
        def download(url: str) -> bytes | None:
            return social.download_image(url, timeout=settings.request_timeout_seconds, user_agent=settings.user_agent)
    options = dict(timeout=settings.request_timeout_seconds, user_agent=settings.user_agent)
    if settings.youtube_api_key:
        try:
            streams = find_live(settings.youtube_channel_id, settings.youtube_api_key, get=get, **options)
        except LiveCheckError as exc:
            # The official route failed (quota, outage): the public pages are still worth a look.
            log.warning("Live check: %s Falling back to the public pages.", exc)
            streams = read_page(settings.youtube_channel_id, **options)
    else:
        streams = read_page(settings.youtube_channel_id, **options)
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
    "OUTCOME_SENT", "api_get", "build_alert", "find_live", "find_live_on_page", "parse_live_page", "parse_watch_page", "recent_stream_ids", "newest_video_ids", "parse_live", "send_live_alerts", "stream_checker",
    "uploads_playlist",
]


def diagnose(channel_id: str, *, timeout: int = 20, user_agent: str = "") -> list[str]:
    """What YouTube's pages look like from where this runs. Reads only; prints no secret."""
    marks = ('"isLiveNow":true', '"isLiveNow":false', '"isLive":true', '"isUpcoming":true', '"isLiveContent":true',
             '"liveBroadcastDetails"', "ytInitialPlayerResponse", "consent.youtube.com", "Sign in to confirm")
    lines = []

    def describe(name: str, url: str) -> None:
        try:
            page = _get_page(url, timeout=timeout, user_agent=user_agent, what=name)
        except LiveCheckError as exc:
            lines.append(f"{name}: {exc}")
            return
        status = re.search(r'"playabilityStatus":\{"status":"([A-Z_]+)"', page)
        canonical = _CANONICAL.search(page)
        kind = "none" if canonical is None else ("watch page" if canonical.group(2) else "channel page")
        lines.append(f"{name}: {len(page)} chars, canonical={kind}, playability={status.group(1) if status else 'none'}, "
                     f"marks={[m for m in marks if m in page]}")

    describe("live page", LIVE_PAGE.format(channel_id=channel_id))
    try:
        candidates = recent_stream_ids(channel_id, timeout=timeout, user_agent=user_agent)
    except social.VideoFeedError as exc:
        lines.append(f"feed: {exc}")
        candidates = []
    lines.append(f"feed: {len(candidates)} candidate video(s)")
    for index, video_id in enumerate(candidates, 1):
        describe(f"video page {index}", WATCH_PAGE.format(video_id=video_id))
    return lines


if __name__ == "__main__":
    _settings = Settings.from_env()
    for _line in diagnose(_settings.youtube_channel_id or "", timeout=_settings.request_timeout_seconds, user_agent=_settings.user_agent):
        print(_line)
