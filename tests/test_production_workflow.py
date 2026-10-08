"""The production workflow DOES send. These tests pin down exactly what it may send, when,
and under which conditions, so a later edit cannot quietly widen it."""

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src import runplan
from src.actuals.models import RELEASE_STATUSES
from src.config import PROJECT_ROOT
from src.runplan import RunPlan, plan

from .test_cloud_runner import SAFE_TEST, SECRET_NAMES, WORKFLOWS, code_lines, workflow_text

PRODUCTION = WORKFLOWS / "production.yml"
IST = ZoneInfo("Asia/Kolkata")
IST_OFFSET_MINUTES = 5 * 60 + 30
GUARD = ("!cancelled() && steps.preflight.outcome == 'success' && steps.destination.outcome != 'failure' "
         "&& steps.collect.outcome == 'success'")


def steps():
    """{step name: step text} for the production workflow, comments removed."""
    text = "\n".join(code_lines(workflow_text(PRODUCTION)))
    parts = re.split(r"\n      - name: ", text)[1:]
    return {part.split("\n", 1)[0]: part for part in parts}


def commands(step_text):
    """The python commands a step runs, whether on the run: line or inside a block."""
    found = []
    for line in step_text.splitlines():
        line = line.strip()
        if line.startswith("run: python -m "):
            found.append(line[len("run: "):])
        elif line.startswith("python -m "):
            found.append(line)
    return found


def expand(field, low, high):
    values = set()
    for part in field.split(","):
        if part == "*":
            values |= set(range(low, high + 1))
        elif "-" in part:
            start, end = part.split("-")
            values |= set(range(int(start), int(end) + 1))
        else:
            values.add(int(part))
    return sorted(values)


def india_minutes(cron):
    """Minutes after India midnight at which a daily cron expression fires, in firing (UTC) order."""
    minute, hour, day, month, weekday = cron.split()
    assert (day, month, weekday) == ("*", "*", "*")
    return [(h * 60 + m + IST_OFFSET_MINUTES) % (24 * 60) for h in expand(hour, 0, 23) for m in expand(minute, 0, 59)]


def hhmm(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def at(hour, minute=0, day=8):
    """A moment given in India time."""
    return datetime(2026, 10, day, hour, minute, tzinfo=IST)


# -- triggers and schedule ---------------------------------------------------------------

def test_triggers_are_the_schedule_and_a_manual_start():
    lines = code_lines(workflow_text(PRODUCTION))
    triggers = []
    for line in lines[lines.index("on:") + 1:]:
        if line and not line.startswith(" "):
            break
        if re.fullmatch(r"  [a-z_]+:.*", line):
            triggers.append(line.strip().rstrip(":"))
    assert triggers == ["schedule", "workflow_dispatch"]
    for forbidden in ("push:", "pull_request", "workflow_run", "repository_dispatch"):
        assert forbidden not in "\n".join(lines)


def test_schedule_in_india_time():
    crons = re.findall(r'- cron: "([^"]+)"', workflow_text(PRODUCTION))
    assert crons == ["15,45 2-19 * * *"]
    times = india_minutes(crons[0])
    assert len(times) == 36 == len(set(times))                       # 36 runs a day
    assert (hhmm(times[0]), hhmm(times[1]), hhmm(times[-1])) == ("07:45", "08:15", "01:15")
    # Every 30 minutes from 07:45 to 01:15 India time, never on the hour.
    unwrapped = [t if t >= times[0] else t + 24 * 60 for t in times]
    assert {b - a for a, b in zip(unwrapped, unwrapped[1:])} == {30}
    assert all(t % 60 in (15, 45) for t in times)
    # Forex Factory allows 2 downloads per 5 minutes: runs are never that close together.
    assert min(b - a for a, b in zip(unwrapped, unwrapped[1:])) >= 5


def test_every_window_has_several_scheduled_runs_so_one_dropped_trigger_is_harmless():
    """GitHub may drop a scheduled run. Each duty must have later runs that would still perform it."""
    times = india_minutes(re.findall(r'- cron: "([^"]+)"', workflow_text(PRODUCTION))[0])
    moments = [datetime(2026, 10, 8, t // 60, t % 60, tzinfo=IST) for t in times]
    morning = [m for m in moments if plan(m).morning]
    evening = [m for m in moments if plan(m).evening]
    assert len(morning) >= 10 and len(evening) >= 4
    assert morning[0].strftime("%H:%M") == "08:15" and evening[0].strftime("%H:%M") == "21:15"
    # Exactly one run in each window performs the destination check (keeps Whapi requests low).
    assert [m.strftime("%H:%M") for m in moments if plan(m).check] == ["08:15", "21:15"]
    # The US release hours (17:30 to 01:30 India time) are polled every half hour.
    release_window = [m for m in moments if m.hour >= 18 or m.hour < 1]
    assert len(release_window) >= 14


def test_both_workflows_share_one_concurrency_group_and_never_cancel():
    for path in (PRODUCTION, SAFE_TEST):
        lines = code_lines(workflow_text(path))
        start = lines.index("concurrency:")
        assert lines[start + 1:start + 3] == ["  group: market-news-automation", "  cancel-in-progress: false"], path.name


# -- the run plan --------------------------------------------------------------------------

@pytest.mark.parametrize("moment, morning, evening, check", [
    (at(7, 45), False, False, False),      # before the morning window: collect only
    (at(8, 14), False, False, False),
    (at(8, 15), True, False, True),        # window opens: brief + alerts, with the destination check
    (at(8, 44), True, False, True),
    (at(8, 45), True, False, False),       # later morning runs catch up without re-checking
    (at(12, 0), True, False, False),
    (at(15, 59), True, False, False),
    (at(16, 0), False, False, False),      # too late in the day for a "morning" brief
    (at(18, 15), False, False, False),     # polling: results only
    (at(21, 14), False, False, False),
    (at(21, 15), False, True, True),       # evening window: tomorrow's reminders
    (at(21, 45), False, True, False),
    (at(23, 59), False, True, False),
    (at(0, 15), False, False, False),      # past midnight: tomorrow has become today
    (at(1, 15), False, False, False),
])
def test_duties_follow_india_time(moment, morning, evening, check):
    result = plan(moment)
    assert (result.morning, result.evening, result.check) == (morning, evening, check)


def test_plan_uses_india_time_whatever_zone_the_moment_is_given_in():
    india = at(8, 15)
    as_utc = india.astimezone(timezone.utc)                      # 02:45 UTC, still the 8th
    as_la = india.astimezone(ZoneInfo("America/Los_Angeles"))    # the evening of the 7th there
    assert as_utc.hour == 2 and as_la.day == 7
    assert plan(india) == plan(as_utc) == plan(as_la) == RunPlan(True, False, True, "Thursday 2026-10-08 08:15")
    # 20:00 UTC is 01:30 the next day in India: outside both windows.
    assert plan(datetime(2026, 10, 7, 20, 0, tzinfo=timezone.utc)).india_time == "Thursday 2026-10-08 01:30"


def test_a_late_or_repeated_run_still_does_the_morning_duties():
    """If the 08:15 trigger is dropped, every later morning run performs the same duties."""
    late = [at(8, 45), at(9, 15), at(11, 45), at(14, 15), at(15, 45)]
    assert all(plan(m).morning for m in late)
    assert not any(plan(m).evening for m in late)


@pytest.mark.parametrize("requested, morning, evening", [
    ("morning", True, False), ("evening", False, True), ("polling", False, False),
])
def test_a_manual_run_can_force_one_kind_of_run(requested, morning, evening):
    for moment in (at(3, 0), at(12, 0), at(22, 0)):               # regardless of the time
        result = plan(moment, requested=requested, manual=True)
        assert (result.morning, result.evening, result.check) == (morning, evening, True)


def test_a_manual_auto_run_follows_the_clock_and_always_checks_the_destination():
    assert plan(at(12, 0), manual=True) == RunPlan(True, False, True, "Thursday 2026-10-08 12:00")
    assert plan(at(18, 0), manual=True) == RunPlan(False, False, True, "Thursday 2026-10-08 18:00")
    assert plan(at(18, 0), manual=False).check is False


def test_unknown_request_is_rejected():
    with pytest.raises(ValueError, match="requested must be one of"):
        plan(at(12, 0), requested="hourly")


def test_runplan_command_prints_outputs_for_the_workflow(monkeypatch, capsys, settings):
    monkeypatch.setattr(runplan.Settings, "from_env", classmethod(lambda cls: settings))

    class Clock(runplan.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 8, 2, 45, tzinfo=timezone.utc).astimezone(tz)      # 08:15 India time

    monkeypatch.setattr(runplan, "datetime", Clock)
    assert runplan.main([]) == 0
    assert capsys.readouterr().out == "morning=true\nevening=false\ncheck=true\nindia_time=Thursday 2026-10-08 08:15\n"
    assert runplan.main(["--requested", "", "--manual"]) == 0          # an empty input means auto
    assert capsys.readouterr().out.startswith("morning=true\nevening=false\ncheck=true\n")
    assert runplan.main(["--requested", "evening"]) == 0
    assert capsys.readouterr().out.startswith("morning=false\nevening=true\ncheck=true\n")
    assert runplan.main(["--requested", "weekly"]) == 2
    assert "CONFIGURATION ERROR" in capsys.readouterr().err


def test_runplan_reads_only_the_clock():
    source = (PROJECT_ROOT / "src" / "runplan.py").read_text(encoding="utf-8")
    for forbidden in ("urllib", "open_database", "whapi", "psycopg", "sqlite"):
        assert forbidden not in source.casefold(), forbidden
    assert "datetime.now(timezone.utc)" in source                     # never the machine's local time


# -- steps ---------------------------------------------------------------------------------

def test_the_plan_step_passes_the_request_and_the_trigger_safely():
    step = steps()["Decide what this run should do"]
    assert "REQUESTED: ${{ inputs.mode }}" in step and "EVENT_NAME: ${{ github.event_name }}" in step
    assert 'python -m src.runplan --requested "${REQUESTED:-auto}" $manual | tee -a "$GITHUB_OUTPUT"' in step
    assert 'if [ "$EVENT_NAME" = "workflow_dispatch" ]; then manual="--manual"; fi' in step
    text = workflow_text(PRODUCTION)
    assert "default: auto" in text
    assert re.findall(r"^          - (\w+)$", text, flags=re.M) == ["auto", "polling", "morning", "evening"]


def test_it_refreshes_and_enriches_before_any_send():
    all_steps = steps()
    assert list(all_steps) == [
        "Check out the repository", "Set up Python", "Install dependencies", "Decide what this run should do",
        "Preflight (reports PRESENT or MISSING, never a value)", "WhatsApp destination check (sends nothing)",
        "Refresh the calendar and look up released figures", "Send the morning update",
        "Send today's high-impact alerts", "Send newly released results", "Send tomorrow's reminders",
        "Fail the run if a source reported a problem"]
    collect = all_steps["Refresh the calendar and look up released figures"]
    assert commands(collect) == ["python -m src.main --enrich-actuals --week > collect.out 2> collect.err"]
    assert "--dry-run" not in collect and "--no-fetch" not in collect
    assert "exit $code" in collect                                    # its own failure fails the step


def test_it_sends_exactly_the_four_existing_messages_for_real():
    all_steps = steps()
    sends = {name: commands(text)[-1] for name, text in all_steps.items() if "--whatsapp-send-" in text}
    assert sends == {
        "Send the morning update": "python -m src.main --whatsapp-send-morning",
        "Send today's high-impact alerts": "python -m src.main --whatsapp-send-alert --today",
        "Send newly released results": 'python -m src.main --whatsapp-send-actuals --from "$since"',
        "Send tomorrow's reminders": "python -m src.main --whatsapp-send-upcoming --tomorrow",
    }
    everything = "\n".join(all_steps.values())
    for forbidden in ("--dry-run", "--fixture", "--whatsapp-test", "--all"):
        assert forbidden not in everything, forbidden
    # Alerts and reminders cover one day each; neither is ever sent for the whole week.
    assert "--week" not in all_steps["Send today's high-impact alerts"]
    assert "--week" not in all_steps["Send tomorrow's reminders"]
    results = all_steps["Send newly released results"]
    assert "timedelta(days=1)" in results and "ZoneInfo('Asia/Kolkata')" in results


def test_each_send_runs_only_when_planned_and_only_after_the_checks_passed():
    all_steps = steps()
    expected = {
        "Send the morning update": " && steps.plan.outputs.morning == 'true'",
        "Send today's high-impact alerts": " && steps.plan.outputs.morning == 'true'",
        "Send newly released results": "",
        "Send tomorrow's reminders": " && steps.plan.outputs.evening == 'true'",
    }
    for name, extra in expected.items():
        assert "if: ${{ " + GUARD + extra + " }}" in all_steps[name], name
    check = all_steps["WhatsApp destination check (sends nothing)"]
    assert "if: steps.plan.outputs.check == 'true'" in check
    assert commands(check) == ["python -m src.main --whatsapp-check"]
    assert commands(all_steps["Preflight (reports PRESENT or MISSING, never a value)"]) == ["python -m src.preflight --production"]


def test_it_fails_loudly_and_never_retries():
    all_steps = steps()
    final = all_steps["Fail the run if a source reported a problem"]
    assert "if: ${{ always() && steps.collect.outcome != 'skipped' }}" in final
    assert "could not refresh from Forex Factory|Live fetch failed" in final
    assert "Release status: .*FAILED [1-9]" in final and "exit $problems" in final
    text = "\n".join(code_lines(workflow_text(PRODUCTION)))
    for forbidden in ("continue-on-error", "retry", "while ", "until ", "sleep", "|| true"):
        assert forbidden not in text, forbidden
    assert "timeout-minutes: 10" in text and "contents: read" in text and "DATABASE_BACKEND: postgres" in text


def test_the_failure_markers_match_what_the_application_prints():
    """The workflow greps for these texts, so they must stay in step with the application."""
    main_source = (PROJECT_ROOT / "src" / "main.py").read_text(encoding="utf-8")
    pipeline_source = (PROJECT_ROOT / "src" / "pipeline.py").read_text(encoding="utf-8")
    assert "could not refresh from Forex Factory" in main_source
    assert "Live fetch failed" in pipeline_source
    assert 'f"Release status: {statuses}"' in main_source
    assert RELEASE_STATUSES[-1] == "FAILED"      # so "FAILED n" is the last item on that line


def test_secrets_are_only_referenced_never_written():
    text = workflow_text(PRODUCTION)
    assert set(re.findall(r"\$\{\{\s*secrets\.([A-Z_]+)\s*\}\}", text)) == SECRET_NAMES
    for line in code_lines(text):
        match = re.fullmatch(r"\s+([A-Z_]+):\s*(.+)", line)
        if match and match.group(1) in SECRET_NAMES:
            assert match.group(2) == "${{ secrets.%s }}" % match.group(1), line
    assert "postgresql://" not in text and "@g.us" not in text
    assert not re.search(r"(printenv|env \||set -x|cat \.env)", "\n".join(code_lines(text)))
    all_steps = steps()
    assert "WHAPI_TOKEN" not in all_steps["Refresh the calendar and look up released figures"]
    assert "secrets." not in all_steps["Decide what this run should do"]
    assert all("WHAPI_TOKEN: ${{ secrets.WHAPI_TOKEN }}" in t for n, t in all_steps.items() if n.startswith("Send "))


def test_workflow_file_is_plain_spaces_with_unix_line_endings():
    raw = PRODUCTION.read_bytes()
    assert b"\t" not in raw and b"\r\n" not in raw


def test_a_day_of_scheduled_runs_stays_inside_the_free_allowances():
    """36 runs a day: about 1,100 runner-minutes a month at one minute each, and 4 Whapi check requests a day."""
    times = india_minutes(re.findall(r'- cron: "([^"]+)"', workflow_text(PRODUCTION))[0])
    assert len(times) * 31 <= 1200
    checks = sum(plan(datetime(2026, 10, 8, t // 60, t % 60, tzinfo=IST)).check for t in times)
    assert checks * 2 * 31 <= 150            # the destination check makes two API requests
    assert timedelta(minutes=30) == timedelta(minutes=30)
