"""Forwards new YouTube Shorts from the owner's channel to Telegram.

    python -m src.main --telegram-send-videos [--dry-run]

Each new Short becomes one Telegram post: its cover image, its title and
description as written on YouTube, and its link. The channel's public feed is
read (no key, no login); nothing is scraped and no video file is downloaded.

A video is posted at most once: its id is the message identity. Videos
published before VIDEO_POSTS_SINCE are ignored, so switching the feature on
never floods the channel with old uploads.
"""

from __future__ import annotations

import logging
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

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

MESSAGE_TYPE = "VIDEO_POST"
FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
CAPTION_LIMIT = 1024  # Telegram's limit for the text under a photo
# A safety limit: if many videos appear at once, the rest follow on the next runs.
MAX_POSTS_PER_RUN = 3
SHORTS, ALL = "shorts", "all"
# Far shorter than the time delivery records are kept (see pipeline.cleanup_old_deliveries).
FORWARD_MAX_AGE = timedelta(days=7)
# How long a new video may wait for its cover image before it is posted without one.
COVER_WAIT = timedelta(minutes=45)
MIN_IMAGE_BYTES, MAX_IMAGE_BYTES = 5_000, 10 * 1024 * 1024
_IMAGE_URL = re.compile(r"^https://i\d?\.ytimg\.com/vi/[A-Za-z0-9_-]{6,20}/[a-z0-9]+\.jpg$")
_CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{6,20}$")
_INSTAGRAM_PROFILE = re.compile(r"^https://(?:www\.)?instagram\.com/([A-Za-z0-9._]{1,30})/?(?:\?.*)?$")
_NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
       "media": "http://search.yahoo.com/mrss/"}


class VideoFeedError(Exception):
    """The channel's feed could not be read."""


@dataclass(frozen=True)
class Video:
    video_id: str
    title: str
    description: str
    link: str
    published: datetime  # UTC
    thumbnail: str | None
    is_short: bool

    @property
    def message_key(self) -> str:
        return f"VIDEO_YT_{self.video_id}"

    @property
    def images(self) -> list[str]:
        """Cover images to try, best first. A Short has an upright cover of its own."""
        base = f"https://i.ytimg.com/vi/{self.video_id}"
        urls = [f"{base}/oar2.jpg"] if self.is_short else []
        urls += [f"{base}/maxresdefault.jpg", f"{base}/hq720.jpg"]
        return urls + ([self.thumbnail] if self.thumbnail else [])


# -- feed -----------------------------------------------------------------------------------

def parse_feed(xml_text: str) -> list[Video]:
    """Read YouTube's Atom feed for a channel. Entries that cannot be read are left out."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise VideoFeedError("YouTube returned a feed that is not valid XML.") from exc
    videos = []
    for entry in root.findall("a:entry", _NS):
        video_id = (entry.findtext("yt:videoId", "", _NS) or "").strip()
        published = (entry.findtext("a:published", "", _NS) or "").strip()
        link = entry.find("a:link[@rel='alternate']", _NS)
        href = (link.get("href") if link is not None else "") or ""
        if not _VIDEO_ID.match(video_id) or not href.startswith("https://www.youtube.com/"):
            continue
        try:
            moment = datetime.fromisoformat(published.replace("Z", "+00:00"))
        except ValueError:
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        thumbnail = entry.find("media:group/media:thumbnail", _NS)
        thumb_url = thumbnail.get("url") if thumbnail is not None else None
        videos.append(Video(
            video_id=video_id,
            title=(entry.findtext("a:title", "", _NS) or "").strip(),
            description=(entry.findtext("media:group/media:description", "", _NS) or "").strip(),
            link=href, published=moment.astimezone(timezone.utc),
            thumbnail=thumb_url if thumb_url and thumb_url.startswith("https://") else None,
            is_short="/shorts/" in href))
    return videos


def fetch_feed(channel_id: str, *, timeout: int = 20, user_agent: str = "") -> list[Video]:
    if not _CHANNEL_ID.match(channel_id or ""):
        raise VideoFeedError("YOUTUBE_CHANNEL_ID is not a YouTube channel id (it starts with UC and has 24 characters).")
    request = urllib.request.Request(FEED_URL.format(channel_id=channel_id), headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise VideoFeedError(f"YouTube's feed answered HTTP {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise VideoFeedError(f"YouTube's feed could not be reached: {type(exc).__name__}.") from exc
    return parse_feed(body)


def parse_since(value: str) -> datetime:
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise ValueError("VIDEO_POSTS_SINCE must be a date and time such as 2026-10-08T00:00:00Z.") from exc
    return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def select_new(videos: list[Video], *, since: datetime, kinds: str = SHORTS, now: datetime | None = None) -> list[Video]:
    """Videos to forward, oldest first.

    Only recent uploads count: the record that a video was posted is deleted
    after a while, and an old video still listed in the feed must not be
    posted a second time once that record is gone.
    """
    now = now or datetime.now(timezone.utc)
    since = max(since, now - FORWARD_MAX_AGE)
    wanted = [v for v in videos if v.published >= since and (kinds == ALL or v.is_short)]
    return sorted(wanted, key=lambda v: (v.published, v.video_id))


# -- text -----------------------------------------------------------------------------------

def clean_profile_url(value: str | None) -> str | None:
    """An Instagram profile address without tracking parameters, or None if it is not one."""
    match = _INSTAGRAM_PROFILE.match((value or "").strip())
    return f"https://www.instagram.com/{match.group(1)}" if match else None


def build_caption(video: Video, instagram_url: str | None = None) -> str:
    """The owner's own words, unchanged, with the links. Shortened only to fit Telegram's limit."""
    header = "🎬 NEW REEL" if video.is_short else "🎬 NEW VIDEO"
    footer = f"▶️ YouTube: {video.link}"
    if instagram_url:
        footer += f"\n📸 Instagram: {instagram_url}"
    body = video.title
    if video.description and video.description != video.title:
        body = f"{body}\n\n{video.description}" if body else video.description
    room = CAPTION_LIMIT - len(header) - len(footer) - 4  # two blank lines on each side
    if len(body) > room:
        body = body[:room - 1].rstrip() + "…"
    return "\n\n".join(part for part in (header, body, footer) if part)


def download_image(url: str, *, timeout: int = 20, user_agent: str = "") -> bytes | None:
    """The cover image at `url`, or None if it is missing, not ready yet, or not a usable JPEG."""
    if not _IMAGE_URL.match(url or ""):
        return None
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            kind = (response.headers.get("Content-Type") or "").lower()
            data = response.read(MAX_IMAGE_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    # YouTube answers a tiny grey placeholder while a cover is still being made.
    if not kind.startswith("image/jpeg") or not (MIN_IMAGE_BYTES <= len(data) <= MAX_IMAGE_BYTES) or data[:2] != b"\xff\xd8":
        return None
    return data


class CoverNotReady(TelegramError):
    """A new video has no cover image yet. The post is made on a later run."""


class _PhotoPost:
    """Lets the delivery service post a photo with a caption.

    The cover is downloaded here and uploaded to Telegram. A freshly published
    video whose cover is not ready is left for the next run. Only for a video
    that still has no usable cover after COVER_WAIT is the caption sent as an
    ordinary message, with link preview on.
    """

    def __init__(self, client: TelegramClient, video: Video, *, now: datetime, download):
        self._client, self._video, self._now, self._download = client, video, now, download

    def send_text(self, chat_id: str, text: str, **_: object) -> str:
        for url in self._video.images:
            image = self._download(url)
            if image is None:
                continue
            try:
                return self._client.send_photo_bytes(chat_id, image, text)
            except (TelegramAuthError, TelegramDestinationError, TelegramNetworkError):
                raise
            except TelegramError as exc:
                log.warning("Telegram did not accept a cover image (%s); trying the next option.", exc)
        if self._now - self._video.published < COVER_WAIT:
            raise CoverNotReady("The video's cover image is not ready yet. It is posted on a later run.")
        return self._client.send_text(chat_id, text, markdown=False, link_preview=True)


# -- sending --------------------------------------------------------------------------------

def send_new_videos(db: EventRepository, client: TelegramClient | None, settings: Settings, chat_id: str, *,
                    dry_run: bool = False, now: datetime | None = None, fetch=fetch_feed, download=None,
                    ) -> list[tuple[Video, DeliveryResult, str]]:
    """Post every new video once. Returns (video, result, caption) for each one considered."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if download is None:
        def download(url: str) -> bytes | None:
            return download_image(url, timeout=settings.request_timeout_seconds, user_agent=settings.user_agent)
    videos = fetch(settings.youtube_channel_id, timeout=settings.request_timeout_seconds, user_agent=settings.user_agent)
    new = select_new(videos, since=parse_since(settings.video_posts_since), kinds=settings.youtube_forward, now=now)
    results, posted = [], 0
    for video in new:
        caption = build_caption(video, clean_profile_url(settings.instagram_profile_url))
        if posted >= MAX_POSTS_PER_RUN and db.get_delivery(video.message_key, PROVIDER_TELEGRAM, chat_id) is None:
            continue  # the next run picks it up
        result = deliver_text(
            db, None if client is None else _PhotoPost(client, video, now=now, download=download),
            message_key=video.message_key, message_type=MESSAGE_TYPE, text=caption, chat_id=chat_id, dry_run=dry_run,
            now=now, provider=PROVIDER_TELEGRAM, destination=DESTINATION_TELEGRAM_CHANNEL, label=describe_chat(chat_id))
        if result.outcome in (OUTCOME_SENT, OUTCOME_FAILED, OUTCOME_DRY_RUN):
            posted += 1
        results.append((video, result, caption))
    return results


__all__ = [
    "ALL", "MAX_POSTS_PER_RUN", "MESSAGE_TYPE", "OUTCOME_ALREADY_SENT", "SHORTS", "Video", "VideoFeedError",
    "CoverNotReady", "build_caption", "clean_profile_url", "download_image", "fetch_feed", "parse_feed", "parse_since", "select_new", "send_new_videos",
]
