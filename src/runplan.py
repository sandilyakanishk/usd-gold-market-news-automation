"""Decides what a scheduled run should do:  python -m src.runplan [--requested auto] [--manual]

GitHub does not guarantee that a scheduled run starts on time, or at all. So
the duties of a run are not tied to which trigger fired; they follow from the
current India time:

    morning window   08:15 - 16:00   send the daily brief
    evening window   21:15 - 24:00   send tomorrow's reminders
    every run                         collect, enrich, send today's alerts and new results

Every send is skipped by the application if it was already made, so a run that
arrives late simply catches up and a run that arrives twice does nothing.

Prints key=value lines for the workflow (morning, evening, check, india_time).
It reads only the clock; it opens no connection and sends nothing.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from .config import Settings

MORNING_START, MORNING_END = time(8, 15), time(16, 0)
EVENING_START = time(21, 15)
# The WhatsApp destination check costs API requests, so it runs once per
# window (in the first half hour) rather than on every run.
CHECK_MINUTES = 30
REQUESTS = ("auto", "polling", "morning", "evening")


@dataclass(frozen=True)
class RunPlan:
    morning: bool  # send the daily brief and today's alerts
    evening: bool  # send tomorrow's reminders
    check: bool  # verify the WhatsApp session and destination first
    india_time: str


def _minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def plan(now: datetime, *, requested: str = "auto", manual: bool = False, display_timezone: str | None = "Asia/Kolkata") -> RunPlan:
    """What a run starting at `now` (any timezone-aware moment) should do."""
    if requested not in REQUESTS:
        raise ValueError(f"requested must be one of: {', '.join(REQUESTS)}")
    local = now.astimezone(ZoneInfo(display_timezone) if display_timezone else timezone.utc)
    minute = local.hour * 60 + local.minute
    in_morning = _minutes(MORNING_START) <= minute < _minutes(MORNING_END)
    in_evening = minute >= _minutes(EVENING_START)

    if requested == "auto":
        morning, evening = in_morning, in_evening
        first_slot = (in_morning and minute < _minutes(MORNING_START) + CHECK_MINUTES) or \
                     (in_evening and minute < _minutes(EVENING_START) + CHECK_MINUTES)
        check = manual or first_slot
    else:
        morning, evening, check = requested == "morning", requested == "evening", True
    return RunPlan(morning, evening, check, local.strftime("%A %Y-%m-%d %H:%M"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.runplan", description="Decide what this run should do.")
    parser.add_argument("--requested", default="auto", help="auto (by India time), polling, morning or evening")
    parser.add_argument("--manual", action="store_true", help="the run was started by hand")
    args = parser.parse_args(argv)
    requested = (args.requested or "auto").strip() or "auto"
    try:
        settings = Settings.from_env()
        result = plan(datetime.now(timezone.utc), requested=requested, manual=args.manual,
                      display_timezone=settings.display_timezone)
    except ValueError as exc:
        print(f"CONFIGURATION ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"morning={'true' if result.morning else 'false'}")
    print(f"evening={'true' if result.evening else 'false'}")
    print(f"check={'true' if result.check else 'false'}")
    print(f"india_time={result.india_time}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
