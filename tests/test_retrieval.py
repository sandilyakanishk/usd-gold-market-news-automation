"""Retrieval, network failure and caching. No test here touches the real network
except the opt-in live test at the bottom."""

import io
import os
import urllib.error

import pytest

from src.collector import forex_factory
from src.collector.forex_factory import (
    NetworkError, RateLimitedError, SourceError, fetch_calendar,
)
from src.collector.parser import MalformedFeedError
from src.database.database import Database
from src.pipeline import load_feed, sync

from .conftest import FEED_URL

RATE_LIMIT_PAGE = "<!DOCTYPE html><html><body><h1>Request Denied</h1>You've exceeded the limit</body></html>"


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_urlopen(body=None, error=None, calls=None):
    def _urlopen(request, timeout=None):
        if calls is not None:
            calls.append(request)
        if error is not None:
            raise error
        return FakeResponse(body.encode("utf-8"))
    return _urlopen


def test_fetch_returns_body_and_sends_user_agent(monkeypatch, feed_text):
    calls = []
    monkeypatch.setattr(forex_factory.urllib.request, "urlopen", fake_urlopen(feed_text, calls=calls))
    assert fetch_calendar(FEED_URL, user_agent="my-agent") == feed_text
    assert len(calls) == 1
    assert calls[0].get_header("User-agent") == "my-agent"


@pytest.mark.parametrize("error", [
    urllib.error.URLError("getaddrinfo failed"),
    TimeoutError("timed out"),
    ConnectionResetError("reset"),
])
def test_network_failure_raises_network_error(monkeypatch, error):
    monkeypatch.setattr(forex_factory.urllib.request, "urlopen", fake_urlopen(error=error))
    with pytest.raises(NetworkError):
        fetch_calendar(FEED_URL)


def test_rate_limit_page_is_detected(monkeypatch):
    monkeypatch.setattr(forex_factory.urllib.request, "urlopen", fake_urlopen(RATE_LIMIT_PAGE))
    with pytest.raises(RateLimitedError):
        fetch_calendar(FEED_URL)


def test_http_429_is_rate_limit_and_other_codes_are_source_errors(monkeypatch):
    def http_error(code):
        return urllib.error.HTTPError(FEED_URL, code, "err", None, None)

    monkeypatch.setattr(forex_factory.urllib.request, "urlopen", fake_urlopen(error=http_error(429)))
    with pytest.raises(RateLimitedError):
        fetch_calendar(FEED_URL)
    monkeypatch.setattr(forex_factory.urllib.request, "urlopen", fake_urlopen(error=http_error(503)))
    with pytest.raises(SourceError):
        fetch_calendar(FEED_URL)


def test_empty_or_html_response_is_source_error(monkeypatch):
    for body in ("", "<html><body>Maintenance</body></html>"):
        monkeypatch.setattr(forex_factory.urllib.request, "urlopen", fake_urlopen(body))
        with pytest.raises(SourceError):
            fetch_calendar(FEED_URL)


def test_cache_prevents_second_request(monkeypatch, settings, feed_text):
    calls = []
    monkeypatch.setattr(forex_factory.urllib.request, "urlopen", fake_urlopen(feed_text, calls=calls))
    first = load_feed(settings)
    second = load_feed(settings)
    assert len(calls) == 1
    assert (first.from_cache, second.from_cache) == (False, True)
    assert second.text == feed_text


def test_network_failure_without_cache_raises(monkeypatch, settings):
    monkeypatch.setattr(forex_factory.urllib.request, "urlopen",
                        fake_urlopen(error=urllib.error.URLError("offline")))
    with pytest.raises(NetworkError):
        load_feed(settings)


def test_network_failure_falls_back_to_cache_with_warning(monkeypatch, settings, feed_text):
    monkeypatch.setattr(forex_factory.urllib.request, "urlopen", fake_urlopen(feed_text))
    load_feed(settings)
    monkeypatch.setattr(forex_factory.urllib.request, "urlopen",
                        fake_urlopen(error=urllib.error.URLError("offline")))
    payload = load_feed(settings, force=True)
    assert payload.from_cache and payload.warning
    assert payload.text == feed_text


def test_sync_stores_only_usd_and_is_idempotent(monkeypatch, settings, feed_text):
    monkeypatch.setattr(forex_factory.urllib.request, "urlopen", fake_urlopen(feed_text))
    with Database(settings.database_path) as db:
        first = sync(settings, db)
        second = sync(settings, db)
        assert first.feed_events == 83
        assert (first.stored_events, first.inserted) == (23, 23)
        assert (second.inserted, second.updated, second.unchanged, second.removed) == (0, 0, 23, 0)
        assert {e.currency for e in db.query_events()} == {"USD"}
    # The unfiltered source data stays on disk for debugging.
    assert settings.raw_cache_path.read_text(encoding="utf-8") == feed_text


def test_sync_with_malformed_feed_leaves_database_untouched(monkeypatch, settings, feed_text):
    monkeypatch.setattr(forex_factory.urllib.request, "urlopen", fake_urlopen(feed_text))
    with Database(settings.database_path) as db:
        sync(settings, db)
        monkeypatch.setattr(forex_factory.urllib.request, "urlopen", fake_urlopen('{"oops": tru'))
        with pytest.raises(MalformedFeedError):
            sync(settings, db, force=True)
        assert db.count() == 23


@pytest.mark.skipif(os.environ.get("RUN_LIVE_TESTS") != "1",
                    reason="live request to Forex Factory; set RUN_LIVE_TESTS=1 to enable")
def test_live_feed_is_reachable_and_parseable(parse):
    result = parse(fetch_calendar(FEED_URL))
    assert result.events and result.skipped == 0
