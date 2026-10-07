"""Gold/XAUUSD relevance classification and USD / Gold query helpers.

Two properties are kept strictly apart:
  * impact          -- Forex Factory's own rating, stored as received
  * gold_relevance  -- our classification, driven by config/gold_relevance.json
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable

from ..collector.models import IMPACT_HIGH, IMPACT_LOW, IMPACT_MEDIUM, Event
from ..database.database import Database

USD = "USD"


def _compile(keywords: Iterable[str]) -> list[re.Pattern]:
    # Whole word/phrase match so "Fed" does not hit "Federal" and "PPI" does not hit "shipping".
    return [re.compile(rf"(?<![A-Za-z0-9]){re.escape(k.strip())}(?![A-Za-z0-9])", re.IGNORECASE)
            for k in keywords if k and k.strip()]


class GoldRelevance:
    """Decides whether an event matters for Gold, from an editable keyword list."""

    def __init__(self, currencies: Iterable[str], keywords: Iterable[str], exclude_keywords: Iterable[str] = ()):
        self.currencies = {c.upper() for c in currencies}
        self._include = _compile(keywords)
        self._exclude = _compile(exclude_keywords)

    @classmethod
    def from_file(cls, path: str | Path) -> "GoldRelevance":
        config = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            currencies=config.get("currencies", [USD]),
            keywords=config.get("keywords", []),
            exclude_keywords=config.get("exclude_keywords", []),
        )

    def is_relevant(self, currency: str, event_name: str) -> bool:
        if currency.upper() not in self.currencies:
            return False
        if any(p.search(event_name) for p in self._exclude):
            return False
        return any(p.search(event_name) for p in self._include)

    __call__ = is_relevant


# -- query helpers --------------------------------------------------------------
# Every helper accepts an optional inclusive date window (YYYY-MM-DD, in the
# stored display timezone).

def get_usd_events(db: Database, date_from: str | None = None, date_to: str | None = None) -> list[Event]:
    return db.query_events(currency=USD, date_from=date_from, date_to=date_to)


def get_high_impact_usd_events(db: Database, date_from: str | None = None, date_to: str | None = None) -> list[Event]:
    return db.query_events(currency=USD, impacts=[IMPACT_HIGH], date_from=date_from, date_to=date_to)


def get_medium_impact_usd_events(db: Database, date_from: str | None = None, date_to: str | None = None) -> list[Event]:
    return db.query_events(currency=USD, impacts=[IMPACT_MEDIUM], date_from=date_from, date_to=date_to)


def get_low_impact_usd_events(db: Database, date_from: str | None = None, date_to: str | None = None) -> list[Event]:
    return db.query_events(currency=USD, impacts=[IMPACT_LOW], date_from=date_from, date_to=date_to)


def get_gold_events(db: Database, date_from: str | None = None, date_to: str | None = None) -> list[Event]:
    return db.query_events(gold_relevance=True, date_from=date_from, date_to=date_to)


def get_high_impact_gold_events(db: Database, date_from: str | None = None, date_to: str | None = None) -> list[Event]:
    return db.query_events(gold_relevance=True, impacts=[IMPACT_HIGH], date_from=date_from, date_to=date_to)


def get_medium_impact_gold_events(db: Database, date_from: str | None = None, date_to: str | None = None) -> list[Event]:
    return db.query_events(gold_relevance=True, impacts=[IMPACT_MEDIUM], date_from=date_from, date_to=date_to)


def get_events_for_date(db: Database, date: str, **filters) -> list[Event]:
    """Events on one date. Extra keyword filters are passed to Database.query_events."""
    return db.query_events(date_from=date, date_to=date, **filters)


def get_events_for_date_range(db: Database, date_from: str, date_to: str, **filters) -> list[Event]:
    """Events between two dates, inclusive."""
    return db.query_events(date_from=date_from, date_to=date_to, **filters)
