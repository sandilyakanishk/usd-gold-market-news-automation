"""The production workflow DOES send. These tests pin down exactly what it may send, when,
and under which conditions, so a later edit cannot quietly widen it."""

import re

from src.actuals.models import RELEASE_STATUSES
from src.config import PROJECT_ROOT

from .test_cloud_runner import SAFE_TEST, SECRET_NAMES, WORKFLOWS, code_lines, workflow_text

PRODUCTION = WORKFLOWS / "production.yml"
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


def india_times(cron):
    """Every HH:MM in India time at which a daily cron expression fires."""
    minute, hour, day, month, weekday = cron.split()
    assert (day, month, weekday) == ("*", "*", "*")
    times = []
    for h in expand(hour, 0, 23):
        for m in expand(minute, 0, 59):
            total = (h * 60 + m + IST_OFFSET_MINUTES) % (24 * 60)
            times.append(f"{total // 60:02d}:{total % 60:02d}")
    return times


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
    assert crons == ["45 2 * * *", "7,37 12-19 * * *", "45 15 * * *"]
    morning, polling, evening = (india_times(c) for c in crons)
    assert morning == ["08:15"]
    assert evening == ["21:15"]
    assert polling == ["17:37", "18:07", "18:37", "19:07", "19:37", "20:07", "20:37", "21:07", "21:37", "22:07",
                       "22:37", "23:07", "23:37", "00:07", "00:37", "01:07"]
    # Polling stays inside roughly 17:30-01:30 India time, every 30 minutes.
    offsets = [(int(t[:2]) * 60 + int(t[3:]) - 17 * 60) % (24 * 60) for t in polling]
    assert offsets == sorted(offsets) and {b - a for a, b in zip(offsets, offsets[1:])} == {30}
    assert 30 <= offsets[0] and offsets[-1] <= 8 * 60 + 30
    everything = morning + polling + evening
    assert not any(t.endswith(":00") for t in everything)         # never on the hour
    assert len(everything) == 18 == len(set(everything))           # 18 runs a day; no all-day polling
    # No two runs closer than five minutes apart (Forex Factory allows 2 downloads per 5 minutes).
    minutes = sorted(int(t[:2]) * 60 + int(t[3:]) for t in everything)
    assert min(b - a for a, b in zip(minutes, minutes[1:])) >= 5


def test_run_type_comes_from_the_schedule_that_fired():
    step = steps()["Decide which run this is"]
    assert '"45 2 * * *") mode=morning ;;' in step
    assert '"45 15 * * *") mode=evening ;;' in step
    assert "*) mode=polling ;;" in step
    assert '"") mode="${REQUESTED:-polling}" ;;' in step            # a manual start defaults to polling
    assert "SCHEDULE: ${{ github.event.schedule }}" in step and "REQUESTED: ${{ inputs.mode }}" in step
    assert "default: polling" in workflow_text(PRODUCTION)


def test_both_workflows_share_one_concurrency_group_and_never_cancel():
    for path in (PRODUCTION, SAFE_TEST):
        lines = code_lines(workflow_text(path))
        start = lines.index("concurrency:")
        assert lines[start + 1:start + 3] == ["  group: market-news-automation", "  cancel-in-progress: false"], path.name


def test_it_refreshes_and_enriches_before_any_send():
    all_steps = steps()
    assert list(all_steps) == [
        "Check out the repository", "Set up Python", "Install dependencies", "Decide which run this is",
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


def test_each_send_runs_only_in_its_run_type_and_only_after_the_checks_passed():
    all_steps = steps()
    expected = {
        "Send the morning update": " && steps.mode.outputs.mode == 'morning'",
        "Send today's high-impact alerts": " && steps.mode.outputs.mode == 'morning'",
        "Send newly released results": "",
        "Send tomorrow's reminders": " && steps.mode.outputs.mode == 'evening'",
    }
    for name, extra in expected.items():
        assert "if: ${{ " + GUARD + extra + " }}" in all_steps[name], name
    check = all_steps["WhatsApp destination check (sends nothing)"]
    assert "if: steps.mode.outputs.mode != 'polling' || github.event_name == 'workflow_dispatch'" in check
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
    assert all("WHAPI_TOKEN: ${{ secrets.WHAPI_TOKEN }}" in t for n, t in all_steps.items() if n.startswith("Send "))


def test_workflow_file_is_plain_spaces_with_unix_line_endings():
    raw = PRODUCTION.read_bytes()
    assert b"\t" not in raw and b"\r\n" not in raw
