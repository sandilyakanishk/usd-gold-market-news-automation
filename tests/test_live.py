"""The live alert: one Telegram post when the YouTube channel goes on air."""

import io
import json
import re
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import live, social
from src.database.database import SQLiteRepository
from src.delivery.models import OUTCOME_ALREADY_SENT, OUTCOME_DRY_RUN, OUTCOME_FAILED, OUTCOME_SENT, PROVIDER_TELEGRAM
from src.delivery.telegram import TelegramDestinationError

CHAT = "@example_channel"
CHANNEL = "UC" + "a" * 22
KEY = "AIza-TEST-KEY-not-real-0000000000000000"
SETTINGS = SimpleNamespace(youtube_channel_id=CHANNEL, youtube_api_key=KEY, instagram_profile_url=None,
                           request_timeout_seconds=5, user_agent="test-agent")
WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "live-check.yml"


def video(video_id, status, title="Gold LIVE session", started="2026-10-08T12:40:54Z", ended=None):
    details = {"actualStartTime": started} if started else {}
    if ended:
        details["actualEndTime"] = ended
    return {"id": video_id, "snippet": {"title": title, "liveBroadcastContent": status}, "liveStreamingDetails": details}


def api(newest, videos, calls=None):
    """A stand-in for the YouTube API: playlistItems lists `newest`, videos returns `videos`."""
    calls = [] if calls is None else calls

    def get(resource, params, api_key, **options):
        calls.append((resource, dict(params), api_key))
        if resource == "playlistItems":
            return {"items": [{"contentDetails": {"videoId": v}} for v in newest]}
        asked = params["id"].split(",")
        return {"items": [v for v in videos if v["id"] in asked]}
    get.calls = calls
    return get


class FakeTelegram:
    def __init__(self, error=None):
        self.photos, self.texts, self.error = [], [], error

    def send_photo_bytes(self, chat_id, image, caption):
        if self.error is not None:
            raise self.error
        self.photos.append((chat_id, image.decode(), caption))
        return str(500 + len(self.photos))

    def send_text(self, chat_id, text, *, markdown=True, link_preview=False):
        self.texts.append((chat_id, text, markdown, link_preview))
        return str(600 + len(self.texts))


def covers(url):
    return url.encode()


def no_covers(url):
    return None


@pytest.fixture
def db():
    with SQLiteRepository(":memory:") as repo:
        yield repo


# -- reading the API -----------------------------------------------------------------------

def test_only_a_broadcast_on_air_counts_as_live():
    data = {"items": [video("LiveNow1234", "live"), video("Upcoming123", "upcoming", started=None),
                      video("Finished123", "none", ended="2026-10-08T13:40:00Z"), video("PlainVideo1", "none", started=None),
                      video("JustEnded12", "live", ended="2026-10-08T13:40:00Z"), video("bad id!", "live"), "junk", {}]}
    streams = live.parse_live(data)
    assert [(s.video_id, s.title) for s in streams] == [("LiveNow1234", "Gold LIVE session")]
    assert streams[0].started_at == datetime(2026, 10, 8, 12, 40, 54, tzinfo=timezone.utc)
    assert streams[0].link == "https://www.youtube.com/watch?v=LiveNow1234" and streams[0].message_key == "LIVE_YT_LiveNow1234"
    assert live.parse_live({}) == [] and live.parse_live({"items": None}) == []


def test_a_check_costs_two_requests_and_asks_about_the_newest_videos():
    get = api(["LiveNow1234", "OldVideo123", "LiveNow1234"], [video("LiveNow1234", "live")])
    assert [s.video_id for s in live.find_live(CHANNEL, KEY, get=get)] == ["LiveNow1234"]
    assert [c[0] for c in get.calls] == ["playlistItems", "videos"]
    assert get.calls[0][1] == {"part": "contentDetails", "playlistId": "UU" + "a" * 22, "maxResults": 10}
    assert get.calls[1][1] == {"part": "snippet,liveStreamingDetails", "id": "LiveNow1234,OldVideo123"}
    # With nothing uploaded there is nothing to ask about.
    empty = api([], [])
    assert live.find_live(CHANNEL, KEY, get=empty) == [] and len(empty.calls) == 1


def test_a_wrong_channel_id_is_refused_before_any_request():
    get = api([], [])
    for bad in ("", None, "@handle", "UCshort"):
        with pytest.raises(live.LiveCheckError):
            live.find_live(bad, KEY, get=get)
    assert get.calls == []


def test_the_key_is_sent_as_a_header_and_never_appears_in_an_error(monkeypatch):
    seen = {}

    def ok(request, timeout=0):
        seen["url"], seen["headers"] = request.full_url, {k.lower(): v for k, v in request.header_items()}
        return io.BytesIO(b'{"items": []}')
    monkeypatch.setattr(live.urllib.request, "urlopen", lambda request, timeout=0: _ctx(ok(request)))
    assert live.api_get("videos", {"part": "snippet", "id": "x"}, KEY) == {"items": []}
    assert KEY not in seen["url"] and seen["headers"]["x-goog-api-key"] == KEY
    assert seen["url"].startswith("https://www.googleapis.com/youtube/v3/videos?")

    def denied(request, timeout=0):
        body = json.dumps({"error": {"errors": [{"reason": "quotaExceeded"}], "message": f"key={KEY} is over quota"}}).encode()
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, io.BytesIO(body))
    monkeypatch.setattr(live.urllib.request, "urlopen", denied)
    with pytest.raises(live.LiveCheckError) as caught:
        live.api_get("videos", {"id": "x"}, KEY)
    assert "HTTP 403 (quotaExceeded)" in str(caught.value) and KEY not in str(caught.value)

    def down(request, timeout=0):
        raise urllib.error.URLError(f"cannot connect with {KEY}")
    monkeypatch.setattr(live.urllib.request, "urlopen", down)
    with pytest.raises(live.LiveCheckError) as caught:
        live.api_get("videos", {"id": "x"}, KEY)
    assert KEY not in str(caught.value)

    monkeypatch.setattr(live.urllib.request, "urlopen", lambda request, timeout=0: _ctx(io.BytesIO(b"<html>")))
    with pytest.raises(live.LiveCheckError):
        live.api_get("videos", {"id": "x"}, KEY)


class _ctx:
    def __init__(self, stream):
        self._stream = stream

    def __enter__(self):
        return self._stream

    def __exit__(self, *exc):
        return False


# -- without a key: the public live page ---------------------------------------------------

def page(canonical, *markers, title="🔴 LIVE FOREX TRADING | XAUUSD &amp; GOLD"):
    return (f'<html><head><meta name="title" content="{title}">'
            f'<link rel="canonical" href="https://www.youtube.com/{canonical}"></head><body><script>var ytInitialPlayerResponse = {{'
            + ",".join(markers) + "};</script></body></html>")


ON_AIR = page("watch?v=LiveNow1234", '"playabilityStatus":{"status":"OK"}', '"isLive":true', '"isLiveContent":true')
SCHEDULED = page("watch?v=Upcoming123", '"playabilityStatus":{"status":"LIVE_STREAM_OFFLINE"}', '"isUpcoming":true', '"isLiveContent":true')
NOTHING_ON = page("channel/" + CHANNEL)
REPLAY = page("watch?v=Finished123", '"playabilityStatus":{"status":"OK"}', '"isLiveContent":true')


def test_the_live_page_tells_on_air_from_scheduled_from_nothing():
    streams = live.parse_live_page(ON_AIR)
    assert [(s.video_id, s.title) for s in streams] == [("LiveNow1234", "🔴 LIVE FOREX TRADING | XAUUSD & GOLD")]
    assert live.parse_live_page(SCHEDULED) == []
    assert live.parse_live_page(NOTHING_ON) == []
    assert live.parse_live_page(REPLAY) == []
    # A scheduled stream never counts, whatever else the page says.
    assert live.parse_live_page(page("watch?v=Upcoming123", '"isLive":true', '"isUpcoming":true')) == []


@pytest.mark.parametrize("strange", [
    "", "<html><body>Before you continue to YouTube</body></html>", "<html>Sorry, unusual traffic</html>",
    '<link rel="canonical" href="https://evil.example/watch?v=LiveNow1234">"isLive":true',
    '<link rel="canonical" href="https://www.youtube.com/watch?v=bad id">"isLive":true',
])
def test_a_page_that_is_not_understood_is_an_error_not_a_guess(strange):
    with pytest.raises(live.LiveCheckError):
        live.parse_live_page(strange)


def test_without_a_key_the_page_is_read_and_the_api_is_not_called(db):
    no_key = SimpleNamespace(**{**vars(SETTINGS), "youtube_api_key": None})
    asked, client = [], FakeTelegram()

    def read_page(channel_id, **options):
        asked.append((channel_id, sorted(options)))
        return live.parse_live_page(ON_AIR)

    def api_must_not_be_used(*args, **kwargs):
        raise AssertionError("the API was called without a key")
    outcomes = [live.send_live_alerts(db, client, no_key, CHAT, get=api_must_not_be_used, read_page=read_page, download=covers)[0][1].outcome
                for _ in range(3)]
    assert outcomes == [OUTCOME_SENT, OUTCOME_ALREADY_SENT, OUTCOME_ALREADY_SENT]
    assert asked[0] == (CHANNEL, ["timeout", "user_agent"]) and len(client.photos) == 1
    assert "XAUUSD & GOLD" in client.photos[0][2] and "watch?v=LiveNow1234" in client.photos[0][2]


def test_with_a_key_the_api_is_used_and_the_page_is_not_read(db):
    def page_must_not_be_read(*args, **kwargs):
        raise AssertionError("the page was read although a key is set")
    get = api(["LiveNow1234"], [video("LiveNow1234", "live")])
    result = live.send_live_alerts(db, FakeTelegram(), SETTINGS, CHAT, get=get, read_page=page_must_not_be_read, download=covers)
    assert result[0][1].outcome == OUTCOME_SENT


def test_reading_the_page_asks_only_youtube_for_the_configured_channel(monkeypatch):
    seen = {}

    def opened(request, timeout=0):
        seen["url"] = request.full_url
        return _ctx(io.BytesIO(NOTHING_ON.encode("utf-8")))
    monkeypatch.setattr(live.urllib.request, "urlopen", opened)
    assert live.find_live_on_page(CHANNEL) == []
    assert seen["url"] == f"https://www.youtube.com/channel/{CHANNEL}/live"
    for bad in ("", None, "@handle", "UCshort", CHANNEL + "/../x"):
        with pytest.raises(live.LiveCheckError):
            live.find_live_on_page(bad)

    def refused(request, timeout=0):
        raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", {}, io.BytesIO(b""))
    monkeypatch.setattr(live.urllib.request, "urlopen", refused)
    with pytest.raises(live.LiveCheckError, match="HTTP 429"):
        live.find_live_on_page(CHANNEL)


# -- text ----------------------------------------------------------------------------------

def test_alert_text():
    stream = live.LiveStream("LiveNow1234", "🔴 LIVE FOREX TRADING | XAUUSD GOLD LIVE", None)
    assert live.build_alert(stream, "https://www.instagram.com/example.handle") == "\n".join([
        "🔴 WE ARE LIVE NOW!",
        "",
        "🔴 LIVE FOREX TRADING | XAUUSD GOLD LIVE",
        "",
        "▶️ Watch live: https://www.youtube.com/watch?v=LiveNow1234",
        "📸 Instagram: https://www.instagram.com/example.handle",
        "",
        "🔔 The stream has just started. Tap the link to join.",
    ])
    plain = live.build_alert(live.LiveStream("LiveNow1234", "", None))
    assert "Instagram" not in plain and plain.startswith("🔴 WE ARE LIVE NOW!\n\n▶️ Watch live: ")
    long_title = live.build_alert(live.LiveStream("LiveNow1234", "x" * 3000, None))
    assert len(long_title) <= social.CAPTION_LIMIT and "https://www.youtube.com/watch?v=LiveNow1234" in long_title


# -- sending -------------------------------------------------------------------------------

def test_a_stream_is_announced_once_however_many_checks_see_it(db):
    client, get = FakeTelegram(), api(["LiveNow1234", "OldVideo123"], [video("LiveNow1234", "live"), video("OldVideo123", "none")])
    outcomes = [live.send_live_alerts(db, client, SETTINGS, CHAT, get=get, download=covers)[0][1].outcome for _ in range(4)]
    assert outcomes == [OUTCOME_SENT, OUTCOME_ALREADY_SENT, OUTCOME_ALREADY_SENT, OUTCOME_ALREADY_SENT]
    assert len(client.photos) == 1 and client.texts == []
    chat, image, caption = client.photos[0]
    assert chat == CHAT and image == "https://i.ytimg.com/vi/LiveNow1234/maxresdefault_live.jpg"
    assert "WE ARE LIVE NOW" in caption and "https://www.youtube.com/watch?v=LiveNow1234" in caption
    assert db.get_delivery("LIVE_YT_LiveNow1234", PROVIDER_TELEGRAM, CHAT).message_type == "LIVE_ALERT"


def test_nothing_is_posted_when_the_channel_is_not_live(db):
    client = FakeTelegram()
    get = api(["Upcoming123", "OldVideo123"], [video("Upcoming123", "upcoming", started=None), video("OldVideo123", "none")])
    assert live.send_live_alerts(db, client, SETTINGS, CHAT, get=get, download=covers) == []
    assert client.photos == [] and client.texts == [] and db.count_deliveries() == 0


def test_the_next_stream_gets_its_own_alert(db):
    client = FakeTelegram()
    live.send_live_alerts(db, client, SETTINGS, CHAT, get=api(["StreamOne11"], [video("StreamOne11", "live")]), download=covers)
    ended = [video("StreamOne11", "none", ended="2026-10-08T14:00:00Z"), video("StreamTwo22", "live", title="Evening session")]
    second = live.send_live_alerts(db, client, SETTINGS, CHAT, get=api(["StreamTwo22", "StreamOne11"], ended), download=covers)
    assert [(s.video_id, r.outcome) for s, r, _ in second] == [("StreamTwo22", OUTCOME_SENT)]
    assert len(client.photos) == 2 and "Evening session" in client.photos[1][2]


def test_an_alert_does_not_wait_for_a_cover(db):
    """Unlike a reel, a live alert is worthless later: without a cover it goes out at once as a link post."""
    client = FakeTelegram()
    result = live.send_live_alerts(db, client, SETTINGS, CHAT, get=api(["LiveNow1234"], [video("LiveNow1234", "live")]), download=no_covers)
    assert result[0][1].outcome == OUTCOME_SENT and client.photos == []
    chat, text, markdown, link_preview = client.texts[0]
    assert "https://www.youtube.com/watch?v=LiveNow1234" in text and markdown is False and link_preview is True


def test_a_telegram_problem_is_recorded_and_the_next_check_tries_again(db):
    get = api(["LiveNow1234"], [video("LiveNow1234", "live")])
    broken = FakeTelegram(error=TelegramDestinationError("Telegram answered 403: bot is not a member"))
    assert live.send_live_alerts(db, broken, SETTINGS, CHAT, get=get, download=covers)[0][1].outcome == OUTCOME_FAILED
    assert broken.texts == []
    client = FakeTelegram()
    assert live.send_live_alerts(db, client, SETTINGS, CHAT, get=get, download=covers)[0][1].outcome == OUTCOME_SENT


def test_dry_run_sends_and_stores_nothing(db):
    result = live.send_live_alerts(db, None, SETTINGS, CHAT, dry_run=True, get=api(["LiveNow1234"], [video("LiveNow1234", "live")]))
    assert result[0][1].outcome == OUTCOME_DRY_RUN and "WE ARE LIVE NOW" in result[0][2] and db.count_deliveries() == 0


def test_live_covers_are_allowed_by_the_image_downloader():
    for url in live.LiveStream("LiveNow1234", "t", None).images:
        assert social._IMAGE_URL.match(url), url


# -- command line --------------------------------------------------------------------------

def _env(monkeypatch, tmp_path, key=KEY, channel=CHANNEL):
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "events.db"))
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "log.txt"))
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    monkeypatch.setenv("YOUTUBE_CHANNEL_ID", channel)
    monkeypatch.setenv("YOUTUBE_API_KEY", key)
    monkeypatch.setenv("INSTAGRAM_PROFILE_URL", "")


def _use(monkeypatch, get):
    monkeypatch.setattr(live.send_live_alerts, "__kwdefaults__", {**live.send_live_alerts.__kwdefaults__, "get": get})


def test_cli_says_not_live_or_shows_the_alert(monkeypatch, tmp_path, capsys):
    from src import main as cli
    _env(monkeypatch, tmp_path)
    _use(monkeypatch, api(["OldVideo123"], [video("OldVideo123", "none")]))
    assert cli.main(["--telegram-send-live", "--dry-run"]) == 0
    assert "Not live." in capsys.readouterr().out
    _use(monkeypatch, api(["LiveNow1234"], [video("LiveNow1234", "live")]))
    assert cli.main(["--telegram-send-live", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "LIVE_YT_LiveNow1234" in out and "WE ARE LIVE NOW" in out and KEY not in out


def test_cli_skips_quietly_without_a_channel_and_reports_api_trouble_without_the_key(monkeypatch, tmp_path, capsys):
    from src import main as cli
    _env(monkeypatch, tmp_path, channel="")
    assert cli.main(["--telegram-send-live"]) == 0
    assert "YOUTUBE_CHANNEL_ID is not set. The live check was skipped." in capsys.readouterr().out

    def broken(resource, params, api_key, **options):
        raise live.LiveCheckError("YouTube's API answered HTTP 403 (quotaExceeded).")
    _env(monkeypatch, tmp_path)
    _use(monkeypatch, broken)
    assert cli.main(["--telegram-send-live", "--dry-run"]) == 1
    captured = capsys.readouterr()
    assert "LIVE CHECK ERROR" in captured.err and "retried on the next run" in captured.err
    assert KEY not in captured.err + captured.out and "Traceback" not in captured.err


# -- the workflow --------------------------------------------------------------------------

def test_the_live_check_workflow_is_small_safe_and_separate():
    text = WORKFLOW.read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    # Started only through the API or by hand; nothing a stranger can trigger.
    assert re.findall(r"^  (\w+):$", code.split("permissions:")[0], flags=re.M) == ["workflow_dispatch"]
    for forbidden in ("schedule:", "cron:", "push:", "pull_request", "repository_dispatch", "|| true", "sleep"):
        assert forbidden not in code, forbidden
    assert "contents: read" in code and "timeout-minutes: 5" in code and "DATABASE_BACKEND: postgres" in code
    # A group of its own, so a run every minute never displaces a waiting production run.
    assert "group: market-news-live-check" in code and "cancel-in-progress: false" in code
    assert "group: market-news-automation" not in code
    assert code.count("python -m src.main") == 1 and "run: python -m src.main --telegram-send-live" in code
    assert "vars.TELEGRAM_ENABLED == 'true' && vars.YOUTUBE_CHANNEL_ID != ''" in code and "continue-on-error: true" in code
    # Secrets come from GitHub Secrets only; WhatsApp is not involved.
    assert "YOUTUBE_API_KEY: ${{ secrets.YOUTUBE_API_KEY }}" in code and "WHAPI" not in code and "--whatsapp" not in code
    assert not re.search(r"AIza[0-9A-Za-z_-]{20,}|UC[A-Za-z0-9_-]{22}", text)
    assert b"\t" not in WORKFLOW.read_bytes() and b"\r\n" not in WORKFLOW.read_bytes()
