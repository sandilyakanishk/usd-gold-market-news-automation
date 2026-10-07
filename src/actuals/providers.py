"""Sources of released values. The enrichment logic only ever talks to ActualDataProvider.

A provider returns raw published observations for named series. Turning an
observation into an event's Actual is the mapping's job, not the provider's.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

log = logging.getLogger(__name__)

Observations = dict[str, dict[date, Decimal]]  # series_id -> observation date -> value


class ProviderError(Exception):
    """The provider could not be reached or answered with an error. Messages carry no credentials."""


@dataclass(frozen=True)
class SeriesRequest:
    series_id: str
    start: date  # earliest observation needed
    end: date  # latest observation needed


class ActualDataProvider(ABC):
    name: str = ""

    @abstractmethod
    def unavailable_reason(self) -> str | None:
        """None when the provider can be used, otherwise why not (e.g. a missing API key)."""

    @abstractmethod
    def fetch(self, requests: list[SeriesRequest]) -> Observations:
        """Published observations for the requested series. Raises ProviderError on failure."""


def _http_json(request: urllib.request.Request, *, timeout: int, source: str, secrets: tuple[str, ...] = ()) -> dict:
    """GET/POST returning parsed JSON. Errors name the source, never the URL, and are scrubbed of secrets."""
    def clean(text: str) -> str:
        for secret in secrets:
            if secret:
                text = text.replace(secret, "***")
        return text

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8", errors="replace")).get("error_message", "")
        except (ValueError, AttributeError, OSError):
            pass
        raise ProviderError(clean(f"{source} answered HTTP {exc.code}. {detail}".strip())) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ProviderError(clean(f"Could not reach {source}: {reason}")) from None
    try:
        data = json.loads(body)
    except ValueError:
        raise ProviderError(f"{source} did not return JSON.") from None
    if not isinstance(data, dict):
        raise ProviderError(f"{source} returned an unexpected response.")
    return data


def _number(text: object) -> Decimal | None:
    try:
        return Decimal(str(text).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None  # BLS uses "-" and FRED uses "." for a missing observation


class BLSProvider(ActualDataProvider):
    """U.S. Bureau of Labor Statistics Public Data API.

    Version 1 needs no key (25 requests a day, 25 series per request). With a
    free registration key version 2 is used (500 requests a day). All series
    for one run go into a single request.
    """

    name = "BLS"
    _V1 = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
    _V2 = "https://api.bls.gov/publicAPI/v2/timeseries/data/"

    def __init__(self, api_key: str | None = None, *, timeout: int = 20, user_agent: str = "usd-gold-calendar-collector"):
        self._key = (api_key or "").strip() or None
        self._timeout = timeout
        self._user_agent = user_agent

    def unavailable_reason(self) -> str | None:
        return None

    def fetch(self, requests: list[SeriesRequest]) -> Observations:
        if not requests:
            return {}
        payload: dict = {
            "seriesid": sorted({r.series_id for r in requests}),
            "startyear": str(min(r.start.year for r in requests)),
            "endyear": str(max(r.end.year for r in requests)),
        }
        if self._key:
            payload["registrationkey"] = self._key
        request = urllib.request.Request(
            self._V2 if self._key else self._V1,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": self._user_agent},
        )
        data = _http_json(request, timeout=self._timeout, source="BLS", secrets=(self._key or "",))
        if data.get("status") != "REQUEST_SUCCEEDED":
            message = "; ".join(str(m) for m in data.get("message") or []) or "no detail given"
            raise ProviderError(f"BLS did not process the request: {message}".replace(self._key or "\0", "***"))

        result: Observations = {}
        for series in (data.get("Results") or {}).get("series") or []:
            observations: dict[date, Decimal] = {}
            for row in series.get("data") or []:
                period, value = str(row.get("period", "")), _number(row.get("value"))
                if value is None or not (period.startswith("M") and period[1:].isdigit() and 1 <= int(period[1:]) <= 12):
                    continue  # skip missing values and annual averages (M13)
                observations[date(int(row["year"]), int(period[1:]), 1)] = value
            result[str(series.get("seriesID"))] = observations
        log.info("BLS: fetched %d series in one request", len(result))
        return result


class FREDProvider(ActualDataProvider):
    """FRED, Federal Reserve Bank of St. Louis. Requires a free API key (FRED_API_KEY).

    This product uses the FRED API but is not endorsed or certified by the
    Federal Reserve Bank of St. Louis.
    """

    name = "FRED"
    _URL = "https://api.stlouisfed.org/fred/series/observations"

    def __init__(self, api_key: str | None = None, *, timeout: int = 20, user_agent: str = "usd-gold-calendar-collector"):
        self._key = (api_key or "").strip() or None
        self._timeout = timeout
        self._user_agent = user_agent

    def unavailable_reason(self) -> str | None:
        return None if self._key else "FRED_API_KEY is not set; this event's source needs that free key."

    def fetch(self, requests: list[SeriesRequest]) -> Observations:
        result: Observations = {}
        merged: dict[str, SeriesRequest] = {}
        for r in requests:
            known = merged.get(r.series_id)
            merged[r.series_id] = r if known is None else SeriesRequest(r.series_id, min(r.start, known.start), max(r.end, known.end))
        for series_id, r in sorted(merged.items()):
            query = urllib.parse.urlencode({
                "series_id": series_id, "api_key": self._key or "", "file_type": "json",
                "observation_start": r.start.isoformat(), "observation_end": r.end.isoformat(),
            })
            request = urllib.request.Request(f"{self._URL}?{query}", headers={"User-Agent": self._user_agent})
            data = _http_json(request, timeout=self._timeout, source="FRED", secrets=(self._key or "",))
            if "observations" not in data:
                raise ProviderError(f"FRED returned no observations for {series_id}.")
            observations: dict[date, Decimal] = {}
            for row in data["observations"]:
                value = _number(row.get("value"))
                try:
                    day = date.fromisoformat(str(row.get("date")))
                except ValueError:
                    continue
                if value is not None:
                    observations[day] = value
            result[series_id] = observations
        log.info("FRED: fetched %d series", len(result))
        return result


def build_providers(settings) -> dict[str, ActualDataProvider]:
    """The providers named in the mapping file, configured from the environment."""
    common = {"timeout": settings.request_timeout_seconds, "user_agent": settings.user_agent}
    return {
        "BLS": BLSProvider(settings.bls_api_key, **common),
        "FRED": FREDProvider(settings.fred_api_key, **common),
    }
