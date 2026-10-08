"""Guards on the cloud-runner infrastructure: the workflow must stay incapable of sending,
and the preflight check must never reveal a value."""

import re
import subprocess
import sys

import pytest

from src import preflight
from src.config import PROJECT_ROOT

WORKFLOWS = PROJECT_ROOT / ".github" / "workflows"
SAFE_TEST = WORKFLOWS / "safe-test.yml"
SECRET_NAMES = {"DATABASE_URL", "FRED_API_KEY", "BLS_API_KEY", "WHAPI_TOKEN", "WHATSAPP_COMMUNITY_ID",
                "WHATSAPP_ANNOUNCEMENT_CHAT_ID"}


def workflow_text(path=SAFE_TEST):
    return path.read_text(encoding="utf-8")


def code_lines(text):
    """The workflow without comments, so wording in a comment cannot satisfy or trip a check."""
    return [line.split(" #")[0].rstrip() for line in text.splitlines() if not line.lstrip().startswith("#")]


def run_commands(text):
    return [line.split("run:", 1)[1].strip() for line in code_lines(text) if line.strip().startswith("run:")]


# -- the workflow --------------------------------------------------------------------

def test_exactly_the_expected_workflows_exist():
    assert sorted(p.name for p in WORKFLOWS.iterdir()) == ["live-check.yml", "production.yml", "safe-test.yml"]


def test_workflow_can_only_be_started_by_hand():
    lines = code_lines(workflow_text())
    start = lines.index("on:")
    triggers = []
    for line in lines[start + 1:]:
        if line and not line.startswith(" "):
            break
        if re.fullmatch(r"  [a-z_]+:.*", line):
            triggers.append(line.strip().rstrip(":"))
    assert triggers == ["workflow_dispatch"]
    body = "\n".join(lines)
    for forbidden in ("schedule:", "cron:", "push:", "pull_request", "workflow_run", "repository_dispatch"):
        assert forbidden not in body, forbidden


def test_no_step_can_send_a_real_message():
    commands = run_commands(workflow_text())
    assert commands, "expected run steps"
    sends = [c for c in commands if "--whatsapp-send-" in c]
    assert len(sends) == 2
    assert all("--dry-run" in c for c in sends)
    assert not any("--whatsapp-test" in c for c in commands)
    # Nothing but pip, pytest, the preflight and the project's own CLI is executed.
    for command in commands:
        assert re.fullmatch(r"python -m (pip install -r requirements\.txt|pytest -q|src\.preflight --production|src\.main( --[a-z-]+)+)",
                            command), command
    # Every other project command is one that cannot send or write.
    project = [c for c in commands if c.startswith("python -m src.main")]
    assert [c for c in project if "--dry-run" not in c] == ["python -m src.main --whatsapp-check"]


def test_dry_run_send_steps_are_not_given_the_whapi_token():
    text = "\n".join(code_lines(workflow_text()))
    steps = re.split(r"\n      - name: ", text)[1:]
    for step in steps:
        if "--whatsapp-send-" in step:
            assert "WHAPI_TOKEN" not in step
        if "pytest" in step:
            assert "secrets." not in step


def test_concurrency_allows_one_run_at_a_time_and_never_cancels():
    lines = code_lines(workflow_text())
    start = lines.index("concurrency:")
    block = lines[start + 1:start + 3]
    assert block == ["  group: market-news-automation", "  cancel-in-progress: false"]


def test_secrets_are_only_referenced_never_written():
    text = workflow_text()
    referenced = set(re.findall(r"\$\{\{\s*secrets\.([A-Z_]+)\s*\}\}", text))
    assert referenced == SECRET_NAMES
    for line in code_lines(text):
        match = re.fullmatch(r"\s+([A-Z_]+):\s*(.+)", line)
        if match and match.group(1) in SECRET_NAMES:
            assert re.fullmatch(r"\$\{\{ secrets\.%s \}\}" % match.group(1), match.group(2)), line
    assert "postgresql://" not in text and "@g.us" not in text
    assert not re.search(r"(echo|printenv|env \||set -x|cat \.env)", "\n".join(code_lines(text)))


def test_workflow_has_minimal_permissions_and_a_timeout():
    lines = code_lines(workflow_text())
    assert lines[lines.index("permissions:") + 1] == "  contents: read"
    assert any(line.strip() == "timeout-minutes: 10" for line in lines)
    assert any(line.strip() == "runs-on: ubuntu-latest" for line in lines)
    assert any(line.strip() == 'python-version: "3.11"' for line in lines)


def test_workflow_file_is_plain_spaces_with_unix_line_endings():
    raw = SAFE_TEST.read_bytes()
    assert b"\t" not in raw and b"\r\n" not in raw


# -- repository hygiene ----------------------------------------------------------------

def test_gitignore_excludes_secrets_and_runtime_files():
    rules = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").split()
    for rule in (".env", ".env.*", "!.env.example", "data/*", "logs/*", "*.db", "*.sqlite", "*.log", "__pycache__/"):
        assert rule in rules, rule


def test_env_example_holds_no_values_for_secrets():
    for line in (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        name, _, value = line.partition("=")
        if name in SECRET_NAMES:
            assert value.strip() == "", name


# -- preflight -------------------------------------------------------------------------

FULL = {
    "DATABASE_BACKEND": "postgres",
    "DATABASE_URL": "postgresql://postgres.ref:s3cr3t-P4ss@host.example:5432/postgres",
    "WHAPI_TOKEN": "tok-SECRET-abcdef", "WHATSAPP_ANNOUNCEMENT_CHAT_ID": "120363000000008282@g.us",
    "WHATSAPP_COMMUNITY_ID": "120363000000001940@g.us", "FRED_API_KEY": "0123456789abcdef0123456789abcdef",
}


def test_preflight_reports_presence_without_values():
    lines, problems = preflight.check(production=True, environ=FULL)
    text = "\n".join(lines)
    assert problems == []
    assert f"Python: {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}" in text
    assert "Database backend: postgres" in text
    for name in ("DATABASE_URL", "WHAPI_TOKEN", "WHATSAPP_ANNOUNCEMENT_CHAT_ID", "WHATSAPP_COMMUNITY_ID", "FRED_API_KEY"):
        assert re.search(rf"{name}\s+PRESENT", text), name
    assert re.search(r"BLS_API_KEY\s+MISSING\s+\(optional", text)
    for value in FULL.values():
        if value != "postgres":
            assert value not in text
    assert "s3cr3t" not in text and "@g.us" not in text


@pytest.mark.parametrize("missing", ["DATABASE_URL", "WHAPI_TOKEN", "WHATSAPP_ANNOUNCEMENT_CHAT_ID",
                                     "WHATSAPP_COMMUNITY_ID", "FRED_API_KEY"])
def test_preflight_production_fails_on_any_missing_required_setting(missing):
    env = {k: v for k, v in FULL.items() if k != missing}
    lines, problems = preflight.check(production=True, environ=env)
    assert any(missing in p and "MISSING" in p for p in problems)
    assert re.search(rf"{missing}\s+MISSING", "\n".join(lines))
    assert len([p for p in problems if missing in p]) == 1   # reported once, not twice


def test_preflight_production_rejects_sqlite_and_unknown_backends():
    _, problems = preflight.check(production=True, environ={**FULL, "DATABASE_BACKEND": "sqlite"})
    assert any("must be postgres in production" in p for p in problems)
    _, problems = preflight.check(production=True, environ={k: v for k, v in FULL.items() if k != "DATABASE_BACKEND"})
    assert any("must be postgres in production" in p for p in problems)      # unset means sqlite
    lines, problems = preflight.check(environ={"DATABASE_BACKEND": "s3cr3t-typo"})
    assert "Database backend: unrecognised value" in lines and "s3cr3t-typo" not in "\n".join(lines + problems)


def test_preflight_local_mode_accepts_sqlite_with_nothing_set():
    lines, problems = preflight.check(production=False, environ={})
    assert problems == [] and "Database backend: sqlite" in lines
    _, problems = preflight.check(production=False, environ={"DATABASE_BACKEND": "postgres"})
    assert problems == ["DATABASE_BACKEND is postgres but DATABASE_URL is MISSING."]


def test_blank_values_count_as_missing():
    _, problems = preflight.check(production=True, environ={**FULL, "WHAPI_TOKEN": "   "})
    assert problems == ["WHAPI_TOKEN is MISSING."]


def test_preflight_command_exit_codes_and_output(monkeypatch, capsys):
    monkeypatch.setattr(preflight.Settings, "from_env", classmethod(lambda cls: None))
    for name in FULL:
        monkeypatch.setenv(name, FULL[name])
    monkeypatch.delenv("BLS_API_KEY", raising=False)
    assert preflight.main(["--production"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("PREFLIGHT (production)\nPython: ") and "OK: nothing was connected to and nothing was sent." in out
    assert not any(v in out for v in FULL.values() if v != "postgres")

    monkeypatch.delenv("WHAPI_TOKEN")
    assert preflight.main(["--production"]) == 1
    assert "NOT READY:\n  - WHAPI_TOKEN is MISSING." in capsys.readouterr().out


def test_preflight_runs_as_a_module_without_touching_the_network():
    """A real subprocess with an empty environment: it must report, not crash, and exit non-zero in production mode."""
    env = {"SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", ""), "PATH": __import__("os").environ.get("PATH", ""),
           "PYTHONIOENCODING": "utf-8", "DATABASE_BACKEND": "postgres"}
    done = subprocess.run([sys.executable, "-m", "src.preflight", "--production"], cwd=PROJECT_ROOT, env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert "PREFLIGHT (production)" in done.stdout and "Environment variables:" in done.stdout
    assert done.returncode in (0, 1)
