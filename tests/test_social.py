"""Forwarding new YouTube Shorts to Telegram: cover image, the owner's caption, the link."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src import social
from src.database.database import SQLiteRepository
from src.delivery.models import OUTCOME_ALREADY_SENT, OUTCOME_DRY_RUN, OUTCOME_FAILED, OUTCOME_SENT, PROVIDER_TELEGRAM
from src.delivery.telegram import TelegramClient, TelegramDestinationError, TelegramError, TelegramMessageError

CHAT = "@example_channel"
CHANNEL = "UC" + "a" * 22
SETTINGS = SimpleNamespace(instagram_profile_url=None, youtube_channel_id=CHANNEL, youtube_forward="shorts", video_posts_since="2026-10-08T00:00:00Z",
                           request_timeout_seconds=5, user_agent="test-agent")


def entry(video_id, title, published, *, short=True, description=""):
    link = f"https://www.youtube.com/shorts/{video_id}" if short else f"https://www.youtube.com/watch?v={video_id}"
    return f"""
 <entry>
  <id>yt:video:{video_id}</id>
  <yt:videoId>{video_id}</yt:videoId>
  <title>{title}</title>
  <link rel="alternate" href="{link}"/>
  <published>{published}</published>
  <media:group>
   <media:title>{title}</media:title>
   <media:thumbnail url="https://i1.ytimg.com/vi/{video_id}/hqdefault.jpg" width="480" height="360"/>
   <media:description>{description}</media:description>
  </media:group>
 </entry>"""


def feed(*entries):
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" '
            'xmlns:media="http://search.yahoo.com/mrss/" xmlns="http://www.w3.org/2005/Atom">\n'
            ' <link rel="alternate" href="https://www.youtube.com/channel/UCxxxx"/>\n <title>Example</title>'
            + "".join(entries) + "\n</feed>")


class FakeTelegram:
    def __init__(self, photo_error=None, bad_images=()):
        self.photos, self.texts, self.photo_error, self.bad_images = [], [], photo_error, set(bad_images)

    def send_photo(self, chat_id, photo_url, caption):
        if self.photo_error is not None:
            raise self.photo_error
        if photo_url in self.bad_images:
            raise TelegramError("Telegram answered 400: Bad Request: failed to get HTTP URL content")
        self.photos.append((chat_id, photo_url, caption))
        return str(200 + len(self.photos))

    def send_text(self, chat_id, text, *, markdown=True, link_preview=False):
        self.texts.append((chat_id, text, markdown, link_preview))
        return str(300 + len(self.texts))


def fetcher(xml_text):
    calls = []

    def fetch(channel_id, *, timeout, user_agent):
        calls.append(channel_id)
        return social.parse_feed(xml_text)
    fetch.calls = calls
    return fetch


@pytest.fixture
def db():
    with SQLiteRepository(":memory:") as repo:
        yield repo


NEW_SHORT = entry("AbCdEfGhIjK", "Gold breaks a key level #xauusd", "2026-10-08T12:00:00+00:00")
OLD_SHORT = entry("OlDsHoRt123", "An older reel", "2026-10-06T13:53:36+00:00")
NEW_LONG = entry("LoNgViDeO12", "Weekly live stream", "2026-10-08T13:00:00+00:00", short=False)


# -- feed ----------------------------------------------------------------------------------

def test_parse_feed_reads_what_is_needed():
    videos = social.parse_feed(feed(NEW_SHORT, NEW_LONG))
    short, long_video = videos
    assert (short.video_id, short.title, short.is_short) == ("AbCdEfGhIjK", "Gold breaks a key level #xauusd", True)
    assert short.link == "https://www.youtube.com/shorts/AbCdEfGhIjK"
    assert short.published == datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
    assert short.images == ["https://i.ytimg.com/vi/AbCdEfGhIjK/oar2.jpg", "https://i1.ytimg.com/vi/AbCdEfGhIjK/hqdefault.jpg"]
    assert long_video.is_short is False and long_video.images == ["https://i1.ytimg.com/vi/LoNgViDeO12/hqdefault.jpg"]
    assert short.message_key == "VIDEO_YT_AbCdEfGhIjK"


def test_parse_feed_skips_entries_it_cannot_trust_and_rejects_non_xml():
    odd = entry("bad id!", "x", "2026-10-08T12:00:00+00:00") + entry("GoOdId12345", "y", "not a date") \
        + entry("GoOdId67890", "z", "2026-10-08T12:00:00+00:00").replace("https://www.youtube.com/shorts/", "https://evil.example/")
    assert social.parse_feed(feed(odd)) == []
    assert social.parse_feed(feed()) == []
    with pytest.raises(social.VideoFeedError):
        social.parse_feed("<html>Sorry")


def test_fetch_refuses_anything_that_is_not_a_channel_id():
    for bad in ("", None, "@wealth", "UCshort", "https://youtube.com/@x", "UC" + "a" * 21 + "/"):
        with pytest.raises(social.VideoFeedError):
            social.fetch_feed(bad)


def test_only_new_shorts_are_selected_oldest_first():
    videos = social.parse_feed(feed(NEW_LONG, entry("ZzLater12345", "Later reel", "2026-10-08T15:00:00+00:00"), OLD_SHORT, NEW_SHORT))
    since = social.parse_since("2026-10-08T00:00:00Z")
    assert [v.video_id for v in social.select_new(videos, since=since)] == ["AbCdEfGhIjK", "ZzLater12345"]
    assert [v.video_id for v in social.select_new(videos, since=since, kinds=social.ALL)] == ["AbCdEfGhIjK", "LoNgViDeO12", "ZzLater12345"]
    with pytest.raises(ValueError):
        social.parse_since("last week")


# -- caption -------------------------------------------------------------------------------

def test_caption_is_the_owners_words_plus_the_link():
    video = social.parse_feed(feed(entry("AbCdEfGhIjK", "Gold at a key level", "2026-10-08T12:00:00+00:00",
                                         description="Watch till the end.\n#xauusd #gold")))[0]
    assert social.build_caption(video) == (
        "🎬 NEW REEL\n\nGold at a key level\n\nWatch till the end.\n#xauusd #gold\n\n"
        "▶️ YouTube: https://www.youtube.com/shorts/AbCdEfGhIjK")
    long_video = social.parse_feed(feed(NEW_LONG))[0]
    assert social.build_caption(long_video).startswith("🎬 NEW VIDEO\n\nWeekly live stream\n\n▶️ YouTube: ")


def test_caption_always_fits_telegrams_limit_and_keeps_the_link():
    video = social.parse_feed(feed(entry("AbCdEfGhIjK", "Title", "2026-10-08T12:00:00+00:00", description="word " * 900)))[0]
    caption = social.build_caption(video)
    assert len(caption) <= social.CAPTION_LIMIT
    assert caption.endswith("▶️ YouTube: https://www.youtube.com/shorts/AbCdEfGhIjK") and "…" in caption


def test_the_instagram_profile_is_added_under_the_youtube_link():
    video = social.parse_feed(feed(NEW_SHORT))[0]
    caption = social.build_caption(video, "https://www.instagram.com/example.handle")
    assert caption.endswith("▶️ YouTube: https://www.youtube.com/shorts/AbCdEfGhIjK\n"
                            "📸 Instagram: https://www.instagram.com/example.handle")
    long_one = social.parse_feed(feed(entry("AbCdEfGhIjK", "T", "2026-10-08T12:00:00+00:00", description="word " * 900)))[0]
    fitted = social.build_caption(long_one, "https://www.instagram.com/example.handle")
    assert len(fitted) <= social.CAPTION_LIMIT and fitted.endswith("https://www.instagram.com/example.handle")


@pytest.mark.parametrize("given, expected", [
    ("https://www.instagram.com/example.handle?stkn=abc123==", "https://www.instagram.com/example.handle"),
    ("https://instagram.com/example_handle/", "https://www.instagram.com/example_handle"),
    (" https://www.instagram.com/example ", "https://www.instagram.com/example"),
    ("https://www.instagram.com/reel/Cxyz123/", None), ("https://evil.example/instagram.com/x", None),
    ("http://www.instagram.com/example", None), ("example.handle", None), ("", None), (None, None),
])
def test_profile_address_is_cleaned_of_tracking_and_checked(given, expected):
    assert social.clean_profile_url(given) == expected


def test_the_configured_profile_reaches_the_post(db):
    client = FakeTelegram()
    settings = SimpleNamespace(**{**vars(SETTINGS), "instagram_profile_url": "https://www.instagram.com/example.handle?stkn=abc"})
    social.send_new_videos(db, client, settings, CHAT, fetch=fetcher(feed(NEW_SHORT)))
    assert client.photos[0][2].endswith("\n📸 Instagram: https://www.instagram.com/example.handle")
    assert "stkn" not in client.photos[0][2]


# -- sending -------------------------------------------------------------------------------

def test_a_new_short_is_posted_once_with_its_upright_cover(db):
    client, fetch = FakeTelegram(), fetcher(feed(OLD_SHORT, NEW_LONG, NEW_SHORT))
    first = social.send_new_videos(db, client, SETTINGS, CHAT, fetch=fetch)
    second = social.send_new_videos(db, client, SETTINGS, CHAT, fetch=fetch)
    assert [(v.video_id, r.outcome) for v, r, _ in first] == [("AbCdEfGhIjK", OUTCOME_SENT)]
    assert [r.outcome for _, r, _ in second] == [OUTCOME_ALREADY_SENT]
    assert len(client.photos) == 1 and client.texts == []
    chat, image, caption = client.photos[0]
    assert chat == CHAT and image == "https://i.ytimg.com/vi/AbCdEfGhIjK/oar2.jpg"
    assert "Gold breaks a key level #xauusd" in caption and "https://www.youtube.com/shorts/AbCdEfGhIjK" in caption
    assert db.get_delivery("VIDEO_YT_AbCdEfGhIjK", PROVIDER_TELEGRAM, CHAT).message_type == "VIDEO_POST"
    assert fetch.calls == [CHANNEL, CHANNEL]


def test_old_uploads_are_never_forwarded(db):
    client = FakeTelegram()
    assert social.send_new_videos(db, client, SETTINGS, CHAT, fetch=fetcher(feed(OLD_SHORT))) == []
    assert client.photos == [] and db.count_deliveries() == 0


def test_cover_image_falls_back_to_the_feed_thumbnail_then_to_a_link_post(db):
    client = FakeTelegram(bad_images=["https://i.ytimg.com/vi/AbCdEfGhIjK/oar2.jpg"])
    social.send_new_videos(db, client, SETTINGS, CHAT, fetch=fetcher(feed(NEW_SHORT)))
    assert [p[1] for p in client.photos] == ["https://i1.ytimg.com/vi/AbCdEfGhIjK/hqdefault.jpg"]

    with SQLiteRepository(":memory:") as other:
        plain = FakeTelegram(photo_error=TelegramMessageError("Telegram answered 400: wrong file identifier/HTTP URL specified"))
        results = social.send_new_videos(other, plain, SETTINGS, CHAT, fetch=fetcher(feed(NEW_SHORT)))
        assert results[0][1].outcome == OUTCOME_SENT and plain.photos == []
        chat, text, markdown, link_preview = plain.texts[0]
        assert "https://www.youtube.com/shorts/AbCdEfGhIjK" in text and markdown is False and link_preview is True


def test_a_real_delivery_problem_is_recorded_and_retried_not_hidden(db):
    broken = FakeTelegram(photo_error=TelegramDestinationError("Telegram answered 403: bot is not a member"))
    failed = social.send_new_videos(db, broken, SETTINGS, CHAT, fetch=fetcher(feed(NEW_SHORT)))
    assert failed[0][1].outcome == OUTCOME_FAILED and broken.texts == []
    client = FakeTelegram()
    assert social.send_new_videos(db, client, SETTINGS, CHAT, fetch=fetcher(feed(NEW_SHORT)))[0][1].outcome == OUTCOME_SENT
    assert len(client.photos) == 1


def test_many_new_videos_are_spread_over_runs(db):
    entries = [entry(f"Video{i:06d}", f"Reel {i}", f"2026-10-08T12:{i:02d}:00+00:00") for i in range(5)]
    client, fetch = FakeTelegram(), fetcher(feed(*entries))
    first = social.send_new_videos(db, client, SETTINGS, CHAT, fetch=fetch)
    assert [v.title for v, r, _ in first if r.outcome == OUTCOME_SENT] == ["Reel 0", "Reel 1", "Reel 2"]
    second = social.send_new_videos(db, client, SETTINGS, CHAT, fetch=fetch)
    assert [v.title for v, r, _ in second if r.outcome == OUTCOME_SENT] == ["Reel 3", "Reel 4"]
    assert len(client.photos) == 5


def test_dry_run_sends_and_stores_nothing(db):
    results = social.send_new_videos(db, None, SETTINGS, CHAT, dry_run=True, fetch=fetcher(feed(NEW_SHORT)))
    assert results[0][1].outcome == OUTCOME_DRY_RUN and "NEW REEL" in results[0][2]
    assert db.count_deliveries() == 0


# -- Telegram client -----------------------------------------------------------------------

def test_send_photo_posts_the_image_address_and_plain_caption(monkeypatch):
    client = TelegramClient("123:TEST")
    calls = []
    monkeypatch.setattr(client, "_call", lambda method, payload=None: calls.append((method, payload)) or {"message_id": 77})
    assert client.send_photo(CHAT, "https://i.ytimg.com/vi/x/oar2.jpg", "caption") == "77"
    assert calls == [("sendPhoto", {"chat_id": CHAT, "photo": "https://i.ytimg.com/vi/x/oar2.jpg", "caption": "caption"})]
    for bad in ((CHAT, "http://insecure.example/a.jpg", "c"), (CHAT, "https://ok.example/a.jpg", "  "),
                (CHAT, "https://ok.example/a.jpg", "x" * 1025)):
        with pytest.raises(TelegramMessageError):
            client.send_photo(*bad)


def test_link_preview_is_off_unless_asked_for(monkeypatch):
    client = TelegramClient("123:TEST")
    calls = []
    monkeypatch.setattr(client, "_call", lambda method, payload=None: calls.append(payload) or {"message_id": 1})
    client.send_text(CHAT, "news", markdown=False)
    client.send_text(CHAT, "video https://www.youtube.com/shorts/x", markdown=False, link_preview=True)
    assert [c["disable_web_page_preview"] for c in calls] == [True, False]


# -- command line --------------------------------------------------------------------------

def _cli_env(monkeypatch, tmp_path, channel=CHANNEL):
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "events.db"))
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "log.txt"))
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    monkeypatch.setenv("YOUTUBE_CHANNEL_ID", channel)
    monkeypatch.setenv("VIDEO_POSTS_SINCE", "2026-10-08T00:00:00Z")
    monkeypatch.setenv("YOUTUBE_FORWARD", "shorts")


def test_cli_dry_run_shows_the_post(monkeypatch, tmp_path, capsys):
    from src import main as cli
    _cli_env(monkeypatch, tmp_path)
    monkeypatch.setattr(social.send_new_videos, "__kwdefaults__",
                        {**social.send_new_videos.__kwdefaults__, "fetch": fetcher(feed(NEW_SHORT, OLD_SHORT))})
    assert cli.main(["--telegram-send-videos", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "VIDEO_YT_AbCdEfGhIjK" in out and "https://www.youtube.com/shorts/AbCdEfGhIjK" in out
    assert "OlDsHoRt123" not in out


def test_cli_does_nothing_without_a_channel_and_reports_feed_trouble_plainly(monkeypatch, tmp_path, capsys):
    from src import main as cli
    _cli_env(monkeypatch, tmp_path, channel="")
    assert cli.main(["--telegram-send-videos", "--dry-run"]) == 0
    assert "YOUTUBE_CHANNEL_ID is not set" in capsys.readouterr().out

    def broken(channel_id, **_):
        raise social.VideoFeedError("YouTube's feed answered HTTP 503.")
    _cli_env(monkeypatch, tmp_path)
    monkeypatch.setattr(social.send_new_videos, "__kwdefaults__", {**social.send_new_videos.__kwdefaults__, "fetch": broken})
    assert cli.main(["--telegram-send-videos", "--dry-run"]) == 1
    err = capsys.readouterr().err
    assert "VIDEO FEED ERROR" in err and "retried on the next run" in err and "Traceback" not in err
