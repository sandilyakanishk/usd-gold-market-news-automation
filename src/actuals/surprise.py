"""Factual comparison of Actual against Forecast. Never a market view."""

from __future__ import annotations

import re
from decimal import Decimal

from .models import ABOVE_FORECAST, BELOW_FORECAST, IN_LINE_WITH_FORECAST, NOT_AVAILABLE

# "0.3%", "-100.8B", "254K", "4.50%", "1,250" -- one number with an optional unit.
_VALUE = re.compile(r"^\s*([-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?|[-+]?\d+(?:\.\d+)?)\s*(%|[KMBT])?\s*$", re.IGNORECASE)
_MAGNITUDE = {"K": Decimal(10) ** 3, "M": Decimal(10) ** 6, "B": Decimal(10) ** 9, "T": Decimal(10) ** 12}


def parse_value(text: str | None) -> tuple[Decimal, str] | None:
    """Return (number, unit) for a calendar value, or None if it is not a single plain number."""
    if not text:
        return None
    match = _VALUE.match(text)
    if not match:
        return None  # e.g. "4.83|2.7", "<0.25%", free text
    return Decimal(match.group(1).replace(",", "")), (match.group(2) or "").upper()


def compare(actual: str | None, forecast: str | None) -> tuple[str, float | None]:
    """Return (surprise_status, surprise_value).

    surprise_value is Actual minus Forecast in their shared unit, and is only
    given when both are quoted in the same unit. If the units differ only in
    magnitude (K against M) the direction is still reported, without a value.
    """
    a, f = parse_value(actual), parse_value(forecast)
    if a is None or f is None:
        return NOT_AVAILABLE, None
    (a_number, a_unit), (f_number, f_unit) = a, f
    if a_unit == f_unit:
        difference = a_number - f_number
        value: float | None = float(difference)
    elif a_unit in _MAGNITUDE and f_unit in _MAGNITUDE:
        difference = a_number * _MAGNITUDE[a_unit] - f_number * _MAGNITUDE[f_unit]
        value = None
    else:
        return NOT_AVAILABLE, None  # a percentage against a count, or one side without a unit
    if difference > 0:
        return ABOVE_FORECAST, value
    if difference < 0:
        return BELOW_FORECAST, value
    return IN_LINE_WITH_FORECAST, value
