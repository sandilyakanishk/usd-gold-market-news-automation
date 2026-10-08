"""Command line entry point:  python -m src.main --today"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .actuals.mapping import MappingConfigError
from .actuals.models import FAILED, NO_DATA, RELEASE_STATUSES, RELEASED
from .actuals.service import EnrichedEvent, EnrichResult, enrich_actuals, load_enriched
from .classification.models import CRITICAL, GOLD_LEVELS, HIGH, LOW, MEDIUM, PRIORITIES
from .classification.rules import RulesConfigError
from .classification.service import ClassifiedEvent, ClassifyResult, classify_events, load_classified
from .collector.forex_factory import FetchError
from .content.builder import build_content_builder
from .content.fixtures import load_preview_fixture
from .content.headlines import HeadlineConfigError
from .content.models import ACTUAL_RESULT, HIGH_ALERT, MORNING_UPDATE, UPCOMING_REMINDER, Message
from .content.templates import TemplateConfigError
from .collector.models import IMPACT_HIGH, IMPACT_LOW, IMPACT_MEDIUM, Event
from .collector.parser import MalformedFeedError
from .config import Settings
from .database.base import DatabaseError
from .database.factory import open_database
from .delivery.models import OUTCOME_ALREADY_SENT, OUTCOME_DRY_RUN, OUTCOME_SENT
from .delivery.models import DeliveryError
from .delivery.service import (
    announcement_chat_id, build_telegram_client, build_whapi_client, deliver_message, deliver_telegram_message,
    send_telegram_test_message, send_test_message, telegram_chat_id,
)
from .delivery.telegram import describe_chat
from . import live, pulse, social
from .delivery.whapi import mask_chat_id
from .filters.gold_usd_filters import USD
from .pipeline import cleanup_old_events, sync

RULE = "-" * 33
PRIORITY_RULE = "-" * 25


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.main",
        description="Forex Factory calendar, filtered to USD and Gold/XAUUSD-relevant events.",
    )
    when = p.add_argument_group("date selection (default: --week)").add_mutually_exclusive_group()
    when.add_argument("--today", action="store_true", help="today's events")
    when.add_argument("--tomorrow", action="store_true", help="tomorrow's events")
    when.add_argument("--week", action="store_true", help="this week, Sunday to Saturday")
    when.add_argument("--date", metavar="YYYY-MM-DD", help="one specific date")
    when.add_argument("--all", action="store_true", help="everything stored in the database")
    p.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD", help="start of a date range")
    p.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD", help="end of a date range")

    what = p.add_argument_group("event selection (default: all USD events)")
    what.add_argument("--usd", action="store_true", help="all USD events")
    what.add_argument("--gold", action="store_true", help="only Gold-relevant events")
    what.add_argument("--high-impact", action="store_true", help="only High impact")
    what.add_argument("--medium-impact", action="store_true", help="only Medium impact")
    what.add_argument("--low-impact", action="store_true", help="only Low impact")
    what.add_argument("--gold-high-impact", action="store_true", help="Gold-relevant AND High impact")
    what.add_argument("--gold-medium-impact", action="store_true", help="Gold-relevant AND Medium impact")

    rank = p.add_argument_group(
        "priority view (editorial classification; not a trading signal)",
        "Any of these classifies the stored events and shows the priority view. "
        "In this view --gold uses the classification's Gold relevance.")
    rank.add_argument("--classify", action="store_true", help="classify stored events and show their priority")
    rank.add_argument("--critical", action="store_true", help="only CRITICAL priority")
    rank.add_argument("--high-priority", action="store_true", help="only HIGH priority")
    rank.add_argument("--medium-priority", action="store_true", help="only MEDIUM priority")
    rank.add_argument("--low-priority", action="store_true", help="only LOW priority")
    rank.add_argument("--highlight", action="store_true", help="only events marked highlight_required")

    actual = p.add_argument_group(
        "actual results view (released values; a factual comparison, not a trading signal)",
        "Shows each event's release status, Actual and comparison with the forecast. "
        "The priority filters above also work here.")
    actual.add_argument("--enrich-actuals", action="store_true",
                        help="ask the mapped official sources for released Actual values, store them, show the view")
    actual.add_argument("--actuals", action="store_true", help="show the stored actual results without contacting any source")
    actual.add_argument("--released", action="store_true", help="only events with a verified Actual")
    actual.add_argument("--missing-actual", action="store_true", help="only past events without an Actual (NO_DATA or FAILED)")
    actual.add_argument("--recheck-released", action="store_true",
                        help="with --enrich-actuals: also re-check released events for revised values")
    actual.add_argument("--dry-run", action="store_true",
                        help="with --enrich-actuals: show what would change and write nothing")

    preview = p.add_argument_group(
        "message previews (text generation only; nothing is sent anywhere and nothing is written)",
        "Builds ready-to-publish text from the stored data. No download, no classification run, no source lookup.")
    preview.add_argument("--preview-morning", action="store_true",
                         help="the daily update for today (or --tomorrow / --date)")
    preview.add_argument("--preview-alert", action="store_true",
                         help="high-impact alerts for upcoming events marked highlight_required")
    preview.add_argument("--preview-actuals", action="store_true", help="result messages for events with a verified Actual")
    preview.add_argument("--preview-upcoming", action="store_true", help="reminders for upcoming high-priority events")
    preview.add_argument("--fixture", action="store_true",
                         help="with a preview: use the bundled sample events instead of the database")

    wa = p.add_argument_group(
        "WhatsApp delivery (sends the content engine's messages, unchanged, to the community announcement group)",
        "A message that was already sent is never sent again. Add --dry-run to see what would be sent without sending.")
    wa.add_argument("--whatsapp-check", action="store_true", help="check the Whapi connection and the destination; sends nothing")
    wa.add_argument("--whatsapp-test", action="store_true", help="send one fixed test message")
    wa.add_argument("--whatsapp-send-morning", action="store_true", help="send the daily update for today (or --tomorrow / --date)")
    wa.add_argument("--whatsapp-send-alert", action="store_true", help="send high-impact alerts")
    wa.add_argument("--whatsapp-send-actuals", action="store_true", help="send result messages for released events")
    wa.add_argument("--whatsapp-send-upcoming", action="store_true", help="send reminders for upcoming high-priority events")

    tg = p.add_argument_group(
        "Telegram delivery (sends the same messages, unchanged, to a Telegram channel)",
        "Independent of WhatsApp: each keeps its own record of what was sent. --dry-run works here too.")
    tg.add_argument("--telegram-check", action="store_true", help="check the bot token and that it may post to the channel; sends nothing")
    tg.add_argument("--telegram-test", action="store_true", help="send one fixed test message")
    tg.add_argument("--telegram-send-morning", action="store_true", help="send the daily update for today (or --tomorrow / --date)")
    tg.add_argument("--telegram-send-alert", action="store_true", help="send high-impact alerts")
    tg.add_argument("--telegram-send-actuals", action="store_true", help="send result messages for released events")
    tg.add_argument("--telegram-send-upcoming", action="store_true", help="send reminders for upcoming high-priority events")
    tg.add_argument("--telegram-send-videos", action="store_true",
                    help="forward new YouTube Shorts from YOUTUBE_CHANNEL_ID (cover image, caption and link)")
    tg.add_argument("--telegram-send-live", action="store_true",
                    help="post an alert if the YouTube channel is live right now (needs YOUTUBE_API_KEY)")
    tg.add_argument("--telegram-send-pulse", action="store_true",
                    help="send the half-hourly market pulse (gold price and next high-impact USD event)")

    p.add_argument("--json", action="store_true", help="print JSON instead of text")
    p.add_argument("--no-fetch", action="store_true", help="read the database only, no network")
    p.add_argument("--force-fetch", action="store_true", help="ignore the local cache (mind the 2 per 5 min limit)")

    admin = p.add_argument_group("database maintenance (each runs on its own and exits)")
    admin.add_argument("--init-db", action="store_true", help="create the tables in the configured database")
    admin.add_argument("--cleanup", action="store_true",
                       help="delete events older than CALENDAR_RETENTION_DAYS")
    admin.add_argument("--migrate-to-postgres", action="store_true",
                       help="copy the local SQLite events into the PostgreSQL database at DATABASE_URL")
    return p


def _valid_date(value: str) -> str:
    return date.fromisoformat(value).isoformat()


def resolve_dates(args: argparse.Namespace, today: date) -> tuple[str | None, str | None]:
    if args.date_from or args.date_to:
        return (_valid_date(args.date_from) if args.date_from else None,
                _valid_date(args.date_to) if args.date_to else None)
    if args.today:
        return today.isoformat(), today.isoformat()
    if args.tomorrow:
        day = today + timedelta(days=1)
        return day.isoformat(), day.isoformat()
    if args.date:
        return _valid_date(args.date), _valid_date(args.date)
    if args.all:
        return None, None
    sunday = today - timedelta(days=(today.weekday() + 1) % 7)
    return sunday.isoformat(), (sunday + timedelta(days=6)).isoformat()


def resolve_filters(args: argparse.Namespace) -> dict:
    impacts = set()
    if args.high_impact or args.gold_high_impact:
        impacts.add(IMPACT_HIGH)
    if args.medium_impact or args.gold_medium_impact:
        impacts.add(IMPACT_MEDIUM)
    if args.low_impact:
        impacts.add(IMPACT_LOW)
    gold = args.gold or args.gold_high_impact or args.gold_medium_impact
    filters: dict = {"impacts": sorted(impacts) or None}
    if gold:
        filters["gold_relevance"] = True
    else:
        filters["currency"] = USD
    return filters


def selected_priorities(args: argparse.Namespace) -> list[str]:
    flags = ((args.critical, CRITICAL), (args.high_priority, HIGH), (args.medium_priority, MEDIUM), (args.low_priority, LOW))
    return [priority for chosen, priority in flags if chosen]


def wants_priority_view(args: argparse.Namespace) -> bool:
    return bool(args.classify or args.highlight or selected_priorities(args))


def format_classified(row: ClassifiedEvent) -> str:
    event, c = row.event, row.classification
    lines = [
        f"{event.date} {event.time}" + (f" ({event.timezone})" if event.timezone else " (timezone unknown)"),
        "",
        event.currency,
        event.event_name,
        "",
        f"Forex Factory Impact: {event.impact.upper()}",
    ]
    if c is None:
        return "\n".join(lines + ["Not classified yet."])
    lines += [
        f"Gold Relevance: {c.gold_relevance_level}",
        f"Category: {c.category}",
        f"Priority: {c.priority}",
        f"Highlight: {'YES' if c.highlight_required else 'NO'}",
        "",
        "Reason:",
        c.gold_relevance_reason,
        c.classification_reason,
    ]
    return "\n".join(lines)


def format_priority_report(rows: list[ClassifiedEvent], result: ClassifyResult) -> str:
    title = "USD / GOLD EVENT PRIORITY"
    lines = [title, "=" * len(title), ""]
    if not rows:
        lines.append("No matching events.")
    else:
        lines.append(f"\n\n{PRIORITY_RULE}\n\n".join(format_classified(r) for r in rows))
        done = [r.classification for r in rows if r.classification]
        levels = " | ".join(f"{level} {sum(c.gold_relevance_level == level for c in done)}" for level in GOLD_LEVELS)
        priorities = " | ".join(f"{p} {sum(c.priority == p for c in done)}" for p in PRIORITIES)
        lines += [
            "", "=" * len(title),
            f"{len(rows)} event(s), {sum(c.gold_relevance for c in done)} Gold-relevant",
            f"Gold relevance: {levels}",
            f"Priority: {priorities}",
            f"Highlight: {sum(c.highlight_required for c in done)}",
        ]
    lines.append(f"Rules version: {result.version}")
    if result.unmatched:
        lines.append("No rule yet for (classified by the default rule): " + "; ".join(result.unmatched))
    return "\n".join(lines)


def wants_actuals_view(args: argparse.Namespace) -> bool:
    return bool(args.enrich_actuals or args.actuals or args.released or args.missing_actual)


def format_enriched(row: EnrichedEvent) -> str:
    event, record, c = row.event, row.record, row.classification
    surprise = record.surprise_status
    if record.surprise_value is not None:
        surprise += f" ({record.surprise_value:+g})"
    source = "-"
    if record.actual_source:
        source = f"{record.actual_source} ({record.actual_source_event}, period {record.actual_period})"
    lines = [
        f"{event.date} {event.time}" + (f" ({event.timezone})" if event.timezone else " (timezone unknown)"),
        "",
        event.currency,
        event.event_name,
        "",
        f"Forex Factory Impact: {event.impact.upper()}",
        f"Priority: {c.priority if c else '-'}",
        f"Previous: {event.previous or '-'}",
        f"Forecast: {event.forecast or '-'}",
        f"Actual: {event.actual or '-'}",
        f"Release Status: {record.release_status}",
        f"Actual Source: {source}",
        f"Surprise: {surprise}",
    ]
    if record.actual_revision > 1:
        lines.append(f"Revision: {record.actual_revision}")
    if record.status_reason:
        lines.append(f"Note: {record.status_reason}")
    return "\n".join(lines)


def format_actuals_report(rows: list[EnrichedEvent], result: EnrichResult | None) -> str:
    title = "USD / GOLD ACTUAL RESULTS"
    lines = [title, "=" * len(title), ""]
    if result is not None and result.dry_run:
        lines += ["DRY RUN: nothing was written to the database.", ""]
    if not rows:
        lines.append("No matching events.")
    else:
        lines.append(f"\n\n{PRIORITY_RULE}\n\n".join(format_enriched(r) for r in rows))
        statuses = " | ".join(f"{s} {sum(r.record.release_status == s for r in rows)}" for s in RELEASE_STATUSES)
        lines += ["", "=" * len(title), f"{len(rows)} event(s)", f"Release status: {statuses}"]
    if result is not None:
        verb = "would be" if result.dry_run else "were"
        calls = ", ".join(f"{name} {count}" for name, count in result.provider_calls.items()) or "none"
        lines.append(f"This run: {result.inserted + result.updated} record(s) {verb} written "
                     f"({result.actuals_written} with a new or revised Actual), {result.unchanged} unchanged. "
                     f"Source requests: {calls}.")
    return "\n".join(lines)


def run_actuals_view(args: argparse.Namespace, settings: Settings, db, date_from: str | None, date_to: str | None) -> int:
    filters = resolve_filters(args)
    priorities = set(selected_priorities(args))
    gold_only = "gold_relevance" in filters

    def selected(row: EnrichedEvent) -> bool:
        """The user's choice of events: impact, priority, Gold relevance, highlight."""
        c = row.classification
        if filters["impacts"] and row.event.impact not in filters["impacts"]:
            return False
        if priorities and not (c and c.priority in priorities):
            return False
        if gold_only and not (c and c.gold_relevance):
            return False
        return not args.highlight or bool(c and c.highlight_required)

    def status_wanted(row: EnrichedEvent) -> bool:
        wanted = ([RELEASED] if args.released else []) + ([NO_DATA, FAILED] if args.missing_actual else [])
        return not wanted or row.record.release_status in wanted

    if not args.dry_run:
        classify_events(settings, db)  # so priorities are available for filtering and display
    result = None
    if args.enrich_actuals:
        # Sources are only asked about the events the user selected.
        chosen = {r.event.event_id for r in load_enriched(db, currency=USD, date_from=date_from, date_to=date_to)
                  if selected(r)}
        result = enrich_actuals(settings, db, date_from=date_from, date_to=date_to, event_ids=chosen,
                                recheck_released=args.recheck_released, dry_run=args.dry_run)
        rows = result.rows
    else:
        rows = [r for r in load_enriched(db, currency=USD, date_from=date_from, date_to=date_to) if selected(r)]
    rows = [r for r in rows if status_wanted(r)]

    if args.json:
        print(json.dumps([r.to_dict() for r in rows], indent=2, ensure_ascii=False))
    else:
        print(format_actuals_report(rows, result))
    return 0


def wants_preview(args: argparse.Namespace) -> bool:
    return bool(args.preview_morning or args.preview_alert or args.preview_actuals or args.preview_upcoming)


def format_messages(groups: list[tuple[str, list[Message]]], fixture: bool) -> str:
    out = []
    if fixture:
        out.append("SAMPLE DATA: these messages are built from bundled sample events, not from the database.\n")
    for message_type, messages in groups:
        if not messages:
            hint = "" if fixture else " Add --fixture to preview this message type with sample events."
            out.append(f"No {message_type} message for the selected events.{hint}\n")
        for message in messages:
            out.append(f"===== {message.message_key} | {message.message_type} =====\n{message.text}\n")
    return "\n".join(out).rstrip("\n")


class InvalidDateError(Exception):
    """A date given on the command line could not be read."""


def build_messages(args: argparse.Namespace, settings: Settings, today: date, db, *, morning: bool, alert: bool,
                   actuals: bool, upcoming: bool) -> list[tuple[str, list[Message]]]:
    """Ask the content engine for the requested message types. Read-only."""
    try:
        date_from, date_to = resolve_dates(args, today)
        day = date.fromisoformat(args.date) if args.date else (today + timedelta(days=1) if args.tomorrow else today)
    except ValueError as exc:
        raise InvalidDateError(str(exc)) from exc

    if args.fixture:
        items, now = load_preview_fixture(settings)
        builder = build_content_builder(settings, now)
        day = now.astimezone(builder.tz).date()
        day_items = items
    else:
        builder = build_content_builder(settings, datetime.now(timezone.utc))
        items = load_enriched(db, now=builder.now, currency=USD, date_from=date_from, date_to=date_to)
        day_items = load_enriched(db, now=builder.now, currency=USD, date_from=day.isoformat(), date_to=day.isoformat())

    groups: list[tuple[str, list[Message]]] = []
    if morning:
        groups.append((MORNING_UPDATE, [builder.morning_update(day_items, day)]))
    if alert:
        groups.append((HIGH_ALERT, builder.high_alerts(items)))
    if actuals:
        groups.append((ACTUAL_RESULT, builder.actual_results(items)))
    if upcoming:
        groups.append((UPCOMING_REMINDER, builder.upcoming_reminders(items)))
    return groups


def wants_whatsapp_send(args: argparse.Namespace) -> bool:
    return bool(args.whatsapp_send_morning or args.whatsapp_send_alert or args.whatsapp_send_actuals
                or args.whatsapp_send_upcoming)


def run_whatsapp_check(settings: Settings) -> int:
    """Verify the Whapi channel and the configured destination. Sends nothing."""
    client = build_whapi_client(settings)
    client.health()
    print("Whapi connection: OK (WhatsApp session authenticated)")
    chat_id = announcement_chat_id(settings)
    print(f"Announcement group: {mask_chat_id(chat_id)}")
    if settings.whatsapp_community_id:
        actual = client.announcement_group_id(settings.whatsapp_community_id)
        if actual != chat_id:
            print("DESTINATION MISMATCH: WHATSAPP_ANNOUNCEMENT_CHAT_ID is not the Announcements group of "
                  f"WHATSAPP_COMMUNITY_ID ({mask_chat_id(settings.whatsapp_community_id)}).", file=sys.stderr)
            return 1
        print(f"Community {mask_chat_id(settings.whatsapp_community_id)}: announcement group confirmed")
    else:
        print("WHATSAPP_COMMUNITY_ID is not set, so the group could not be cross-checked against its community.")
    print("Nothing was sent.")
    return 0


def run_whatsapp_test(settings: Settings) -> int:
    client = build_whapi_client(settings)
    chat_id = announcement_chat_id(settings)
    with open_database(settings) as db:
        result = send_test_message(db, client, chat_id)
    if result.outcome != OUTCOME_SENT:
        print(f"WHATSAPP ERROR: test message not sent: {result.error}", file=sys.stderr)
        return 1
    print(f"Test message sent to {mask_chat_id(chat_id)}.")
    print(f"message_key: {result.message_key}")
    print(f"Whapi message ID: {result.provider_message_id}")
    return 0


def _send_messages(args: argparse.Namespace, settings: Settings, today: date, *, kinds: dict, chat_id: str,
                   make_client, deliver, destination_text: str, id_label: str) -> int:
    """Deliver the content engine's messages to one destination. Text is passed on unchanged; duplicates are skipped."""
    client = None if args.dry_run else make_client(settings)
    failed = False
    with open_database(settings) as db:
        try:
            groups = build_messages(args, settings, today, db, **kinds)
        except InvalidDateError as exc:
            print(f"Invalid date: {exc}", file=sys.stderr)
            return 2
        if args.dry_run:
            print("DRY RUN: nothing is sent and no delivery is recorded.\n")
        for message_type, messages in groups:
            # A daily update with no events is not worth a post.
            eligible = [m for m in messages if not (m.message_type == MORNING_UPDATE and not m.events)]
            if not eligible:
                print(f"No eligible {message_type} message.\n")
                continue
            for message in eligible:
                result = deliver(db, client, message, chat_id, dry_run=args.dry_run)
                print(f"===== {message.message_key} | {message.message_type} =====")
                print(f"Destination: {destination_text}")
                if result.outcome == OUTCOME_ALREADY_SENT:
                    print(f"Already sent — skipped. (sent {result.sent_at}, {id_label} {result.provider_message_id})\n")
                elif result.outcome == OUTCOME_DRY_RUN:
                    print(f"Would send:\n{message.text}\n")
                elif result.outcome == OUTCOME_SENT:
                    print(f"Sent. {id_label}: {result.provider_message_id}\n")
                else:
                    failed = True
                    print(f"FAILED: {result.error}\n")
    return 1 if failed else 0


def run_whatsapp_send(args: argparse.Namespace, settings: Settings, today: date) -> int:
    chat_id = announcement_chat_id(settings)
    return _send_messages(
        args, settings, today, chat_id=chat_id, make_client=build_whapi_client, deliver=deliver_message,
        kinds=dict(morning=args.whatsapp_send_morning, alert=args.whatsapp_send_alert,
                   actuals=args.whatsapp_send_actuals, upcoming=args.whatsapp_send_upcoming),
        destination_text=f"whatsapp_community_announcement ({mask_chat_id(chat_id)}) via whapi", id_label="Whapi message ID")


def wants_telegram_send(args: argparse.Namespace) -> bool:
    return bool(args.telegram_send_morning or args.telegram_send_alert or args.telegram_send_actuals
                or args.telegram_send_upcoming)


def run_telegram_check(settings: Settings) -> int:
    """Verify the bot token and that the bot may post to the configured channel. Sends nothing."""
    client = build_telegram_client(settings)
    chat_id = telegram_chat_id(settings)
    info = client.check_destination(chat_id)
    print(f"Telegram bot: OK (@{info['bot']})")
    print(f"Channel: {describe_chat(chat_id)} ({info['type']}), bot is {info['status']} and may post")
    print("Nothing was sent.")
    return 0


def run_telegram_test(settings: Settings) -> int:
    client = build_telegram_client(settings)
    chat_id = telegram_chat_id(settings)
    with open_database(settings) as db:
        result = send_telegram_test_message(db, client, chat_id)
    if result.outcome != OUTCOME_SENT:
        print(f"TELEGRAM ERROR: test message not sent: {result.error}", file=sys.stderr)
        return 1
    print(f"Test message sent to {describe_chat(chat_id)}.")
    print(f"message_key: {result.message_key}")
    print(f"Telegram message ID: {result.provider_message_id}")
    return 0


def run_telegram_send(args: argparse.Namespace, settings: Settings, today: date) -> int:
    chat_id = telegram_chat_id(settings)
    return _send_messages(
        args, settings, today, chat_id=chat_id, make_client=build_telegram_client, deliver=deliver_telegram_message,
        kinds=dict(morning=args.telegram_send_morning, alert=args.telegram_send_alert,
                   actuals=args.telegram_send_actuals, upcoming=args.telegram_send_upcoming),
        destination_text=f"telegram_channel ({describe_chat(chat_id)}) via telegram", id_label="Telegram message ID")


def run_telegram_pulse(args: argparse.Namespace, settings: Settings, today: date) -> int:
    """Post the market pulse for the current half-hour slot. Telegram only."""
    chat_id = telegram_chat_id(settings)
    client = None if args.dry_run else build_telegram_client(settings)
    with open_database(settings) as db:
        try:
            result = pulse.send_pulse(db, client, settings, chat_id, dry_run=args.dry_run)
        except pulse.PriceError as exc:
            logging.getLogger(__name__).warning("Market pulse: %s", exc)
            print(f"PRICE SOURCE ERROR: {exc} The pulse is retried on the next run.", file=sys.stderr)
            return 1
    if result.outcome == pulse.OUTCOME_MARKET_CLOSED:
        print("The gold market is closed for the weekend. No pulse was sent.")
        return 0
    if result.outcome == pulse.OUTCOME_STALE_PRICE:
        print("The price source has not updated recently. No pulse was sent.")
        return 0
    print(f"===== {result.message_key} | {pulse.MESSAGE_TYPE} =====")
    print(f"Destination: telegram_channel ({describe_chat(chat_id)}) via telegram")
    if result.outcome == OUTCOME_ALREADY_SENT:
        print(f"Already sent — skipped. (sent {result.sent_at}, Telegram message ID {result.provider_message_id})")
    elif result.outcome == OUTCOME_DRY_RUN:
        print(f"DRY RUN: nothing is sent and nothing is recorded.\nWould send:\n{result.text}")
    elif result.outcome == OUTCOME_SENT:
        print(f"Sent. Telegram message ID: {result.provider_message_id}")
    else:
        print(f"FAILED: {result.error}")
        return 1
    return 0


def run_telegram_videos(args: argparse.Namespace, settings: Settings, today: date) -> int:
    """Forward the channel's new YouTube Shorts. Telegram only."""
    if not settings.youtube_channel_id:
        print("YOUTUBE_CHANNEL_ID is not set. No video was forwarded.")
        return 0
    chat_id = telegram_chat_id(settings)
    client = None if args.dry_run else build_telegram_client(settings)
    with open_database(settings) as db:
        try:
            results = social.send_new_videos(db, client, settings, chat_id, dry_run=args.dry_run)
        except social.VideoFeedError as exc:
            logging.getLogger(__name__).warning("Video forwarding: %s", exc)
            print(f"VIDEO FEED ERROR: {exc} It is retried on the next run.", file=sys.stderr)
            return 1
    if args.dry_run:
        print("DRY RUN: nothing is sent and nothing is recorded.\n")
    if not results:
        print("No new video.")
        return 0
    failed = False
    for video, result, caption in results:
        print(f"===== {video.message_key} | {social.MESSAGE_TYPE} =====")
        if result.outcome == OUTCOME_ALREADY_SENT:
            print(f"Already sent — skipped. (sent {result.sent_at}, Telegram message ID {result.provider_message_id})\n")
        elif result.outcome == OUTCOME_DRY_RUN:
            print(f"Would send (with the cover image):\n{caption}\n")
        elif result.outcome == OUTCOME_SENT:
            print(f"Sent. Telegram message ID: {result.provider_message_id}\n")
        else:
            failed = True
            print(f"FAILED: {result.error}\n")
    return 1 if failed else 0


def run_telegram_live(args: argparse.Namespace, settings: Settings, today: date) -> int:
    """Post an alert for a YouTube broadcast that is on air. Telegram only."""
    for name, value in (("YOUTUBE_CHANNEL_ID", settings.youtube_channel_id), ("YOUTUBE_API_KEY", settings.youtube_api_key)):
        if not value:
            print(f"{name} is not set. The live check was skipped.")
            return 0
    chat_id = telegram_chat_id(settings)
    client = None if args.dry_run else build_telegram_client(settings)
    with open_database(settings) as db:
        try:
            results = live.send_live_alerts(db, client, settings, chat_id, dry_run=args.dry_run)
        except live.LiveCheckError as exc:
            logging.getLogger(__name__).warning("Live check: %s", exc)
            print(f"LIVE CHECK ERROR: {exc} It is retried on the next run.", file=sys.stderr)
            return 1
    if not results:
        print("Not live.")
        return 0
    failed = False
    for stream, result, text in results:
        print(f"===== {stream.message_key} | {live.MESSAGE_TYPE} =====")
        if result.outcome == OUTCOME_ALREADY_SENT:
            print(f"Live, already announced. (sent {result.sent_at}, Telegram message ID {result.provider_message_id})")
        elif result.outcome == OUTCOME_DRY_RUN:
            print(f"DRY RUN: nothing is sent and nothing is recorded.\nWould send (with the stream's cover):\n{text}")
        elif result.outcome == OUTCOME_SENT:
            print(f"Sent. Telegram message ID: {result.provider_message_id}")
        else:
            failed = True
            print(f"FAILED: {result.error}")
    return 1 if failed else 0


def run_preview(args: argparse.Namespace, settings: Settings, today: date) -> int:
    """Generate message text. Read-only: no sync, no classification run, no enrichment, no delivery."""
    kinds = dict(morning=args.preview_morning, alert=args.preview_alert, actuals=args.preview_actuals,
                 upcoming=args.preview_upcoming)
    try:
        if args.fixture:
            groups = build_messages(args, settings, today, None, **kinds)
        else:
            with open_database(settings) as db:
                groups = build_messages(args, settings, today, db, **kinds)
    except InvalidDateError as exc:
        print(f"Invalid date: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps([m.to_dict() for _, messages in groups for m in messages], indent=2, ensure_ascii=False))
    else:
        print(format_messages(groups, args.fixture))
    return 0


def format_event(event: Event) -> str:
    return "\n".join([
        f"{event.date} {event.time}" + (f" ({event.timezone})" if event.timezone else " (timezone unknown)"),
        event.currency,
        event.event_name,
        f"Impact: {event.impact.upper()}",
        f"Gold Relevance: {'YES' if event.gold_relevance else 'NO'}",
        f"Forecast: {event.forecast or '-'}",
        f"Previous: {event.previous or '-'}",
        f"Actual: {event.actual or '-'}",
    ])


def format_report(events: list[Event]) -> str:
    title = "FOREX FACTORY — USD / GOLD EVENTS"
    lines = [title, "=" * len(title), ""]
    if not events:
        lines.append("No matching events.")
    else:
        lines.append(f"\n\n{RULE}\n\n".join(format_event(e) for e in events))
        high = sum(e.is_high_impact for e in events)
        lines += ["", "=" * len(title), f"{len(events)} event(s), {high} high impact"]
    return "\n".join(lines)


def run_maintenance(args: argparse.Namespace, settings: Settings, today: date) -> bool:
    """Run a maintenance command if one was requested. Returns True if it handled the call."""
    if args.migrate_to_postgres:
        from .database.migrate import migrate_sqlite_to_postgres

        result = migrate_sqlite_to_postgres(settings)
        print(f"Migration complete: {result.source_events} event(s) in SQLite, {result.copied} copied, "
              f"{result.already_present} already in PostgreSQL. PostgreSQL now holds {result.target_events}.")
        return True
    if args.init_db:
        with open_database(settings, require_schema=False) as db:
            db.init_schema()
            print(f"Database ready: {db.describe()}")
        return True
    if args.cleanup:
        with open_database(settings) as db:
            removed = cleanup_old_events(settings, db, today)
        cutoff = today - timedelta(days=settings.calendar_retention_days)
        print(f"Removed {removed} event(s) dated before {cutoff.isoformat()} "
              f"(retention: {settings.calendar_retention_days} days).")
        return True
    return False


def run_report(args: argparse.Namespace, settings: Settings, today: date) -> int:
    try:
        date_from, date_to = resolve_dates(args, today)
    except ValueError as exc:
        print(f"Invalid date: {exc}", file=sys.stderr)
        return 2

    with open_database(settings) as db:
        if not args.no_fetch and not args.dry_run:
            try:
                result = sync(settings, db, force=args.force_fetch)
            except (FetchError, MalformedFeedError) as exc:
                logging.getLogger(__name__).error("Sync failed: %s", exc)
                print(f"WARNING: could not refresh from Forex Factory: {exc}", file=sys.stderr)
                print("Showing what is already stored in the database.\n", file=sys.stderr)
            else:
                if result.warning:
                    print(f"WARNING: {result.warning}\n", file=sys.stderr)
                cleanup_old_events(settings, db, today)
        if wants_actuals_view(args):
            return run_actuals_view(args, settings, db, date_from, date_to)
        if wants_priority_view(args):
            filters = resolve_filters(args)
            result = classify_events(settings, db)
            rows = load_classified(
                db, date_from=date_from, date_to=date_to, currency=USD, impacts=filters["impacts"],
                priorities=selected_priorities(args) or None,
                gold_only="gold_relevance" in filters, highlight_only=args.highlight,
            )
            if args.json:
                print(json.dumps([r.to_dict() for r in rows], indent=2, ensure_ascii=False))
            else:
                print(format_priority_report(rows, result))
            return 0
        events = db.query_events(date_from=date_from, date_to=date_to, **resolve_filters(args))

    if args.json:
        print(json.dumps([e.to_dict() for e in events], indent=2, ensure_ascii=False))
    else:
        print(format_report(events))
    return 0


def _guarded(run, name: str, args: argparse.Namespace, settings: Settings, today: date) -> int:
    """Run one channel's send, turning its configuration or connection error into an exit code."""
    try:
        return run(args, settings, today)
    except DeliveryError as exc:
        logging.getLogger(__name__).error("%s error: %s", name.capitalize(), exc)
        print(f"{name} ERROR: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        print(f"CONFIGURATION ERROR: {exc}", file=sys.stderr)
        return 2

    settings.log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=settings.log_path, encoding="utf-8", level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if args.recheck_released and not args.enrich_actuals:
        print("--dry-run and --recheck-released only apply together with --enrich-actuals.", file=sys.stderr)
        return 2
    sending = wants_whatsapp_send(args) or wants_telegram_send(args)
    if args.dry_run and not (args.enrich_actuals or sending or args.telegram_send_pulse or args.telegram_send_videos
                                 or args.telegram_send_live):
        print("--dry-run and --recheck-released only apply together with --enrich-actuals "
              "(--dry-run also with a --whatsapp-send-... or --telegram-send-... option).", file=sys.stderr)
        return 2
    if args.fixture and sending and not args.dry_run:
        print("Sample events (--fixture) can only be used with --dry-run when sending to WhatsApp or Telegram.", file=sys.stderr)
        return 2

    tz = ZoneInfo(settings.display_timezone) if settings.display_timezone else timezone.utc
    today = datetime.now(tz).date()
    if args.fixture and not (wants_preview(args) or sending):
        print("--fixture only applies together with a --preview-... option.", file=sys.stderr)
        return 2

    try:
        if args.whatsapp_check:
            return run_whatsapp_check(settings)
        if args.whatsapp_test:
            return run_whatsapp_test(settings)
        if args.telegram_check:
            return run_telegram_check(settings)
        if args.telegram_test:
            return run_telegram_test(settings)
        if args.telegram_send_live:
            return _guarded(run_telegram_live, "TELEGRAM", args, settings, today)
        if args.telegram_send_videos:
            return _guarded(run_telegram_videos, "TELEGRAM", args, settings, today)
        if args.telegram_send_pulse:
            return _guarded(run_telegram_pulse, "TELEGRAM", args, settings, today)
        if sending:
            # WhatsApp and Telegram are independent: one failing does not stop the other.
            codes = []
            if wants_whatsapp_send(args):
                codes.append(_guarded(run_whatsapp_send, "WHATSAPP", args, settings, today))
            if wants_telegram_send(args):
                codes.append(_guarded(run_telegram_send, "TELEGRAM", args, settings, today))
            return max(codes)
        if wants_preview(args):
            return run_preview(args, settings, today)
        if run_maintenance(args, settings, today):
            return 0
        return run_report(args, settings, today)
    except (RulesConfigError, MappingConfigError, TemplateConfigError, HeadlineConfigError) as exc:
        logging.getLogger(__name__).error("Configuration file error: %s", exc)
        print(f"CONFIGURATION ERROR: {exc}", file=sys.stderr)
        return 2
    except DeliveryError as exc:
        name = "TELEGRAM" if type(exc).__name__.startswith("Telegram") else "WHATSAPP"
        logging.getLogger(__name__).error("%s error: %s", name.capitalize(), exc)
        print(f"{name} ERROR: {exc}", file=sys.stderr)
        return 1
    except DatabaseError as exc:
        logging.getLogger(__name__).error("Database error: %s", exc)
        print(f"DATABASE ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
