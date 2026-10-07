"""Fetches Forex Factory's official weekly calendar export.

Forex Factory publishes the export at nfs.faireconomy.media (linked from the
calendar page) and limits it to 2 downloads per 5 minutes; the file itself is
refreshed about once an hour. This module makes a single plain GET per call and
never retries -- callers are expected to cache (see src/pipeline.py).
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request

log = logging.getLogger(__name__)


class FetchError(Exception):
    """Base class for anything that stops us getting the feed."""


class NetworkError(FetchError):
    """DNS failure, timeout, connection reset, etc."""


class RateLimitedError(FetchError):
    """Forex Factory refused the request because of its export rate limit."""


class SourceError(FetchError):
    """The source answered, but not with the calendar feed."""


def fetch_calendar(url: str, *, timeout: int = 20, user_agent: str = "usd-gold-calendar-collector") -> str:
    """Return the raw feed body as text. Raises a FetchError subclass on failure."""
    request = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise RateLimitedError(f"HTTP 429 from {url}") from exc
        raise SourceError(f"HTTP {exc.code} from {url}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise NetworkError(f"Could not reach {url}: {exc}") from exc

    stripped = body.lstrip()
    if not stripped:
        raise SourceError(f"Empty response from {url}")
    if stripped.startswith("<"):
        # The rate-limit notice is an HTML page served with a 200 status.
        if "Request Denied" in body or "exceeded the limit" in body:
            raise RateLimitedError("Forex Factory calendar export rate limit hit (2 requests / 5 minutes)")
        raise SourceError(f"Expected JSON from {url} but received HTML")
    return body
