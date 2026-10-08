"""Preflight check for a runner:  python -m src.preflight [--production]

Reports the Python version, the configured database backend and whether each
setting is PRESENT or MISSING. It never prints a value, opens no connection
and sends nothing.

With --production it also insists on the cloud configuration: PostgreSQL as
the backend and every setting the automation needs.
"""

from __future__ import annotations

import argparse
import os
import sys

from .config import PROJECT_ROOT, Settings

MINIMUM_PYTHON = (3, 10)

# (environment variable, what it is for, required in production)
VARIABLES = (
    ("DATABASE_BACKEND", "selects the database: sqlite or postgres", True),
    ("DATABASE_URL", "PostgreSQL connection string (Supabase)", True),
    ("WHAPI_TOKEN", "Whapi.Cloud channel token", True),
    ("WHATSAPP_ANNOUNCEMENT_CHAT_ID", "WhatsApp Announcements group messages are sent to", True),
    ("WHATSAPP_COMMUNITY_ID", "WhatsApp Community, used to confirm the group", True),
    ("FRED_API_KEY", "FRED key for PCE, GDP, retail sales, claims, Fed funds", True),
    ("BLS_API_KEY", "optional BLS key (raises the daily request limit)", False),
    ("TELEGRAM_BOT_TOKEN", "optional: Telegram bot token, for posting to a channel", False),
    ("TELEGRAM_CHAT_ID", "optional: Telegram channel the bot posts to", False),
    ("GEMINI_API_KEY", "optional: free AI writer for the education cards", False),
    ("YOUTUBE_API_KEY", "optional: YouTube Data API key; the live alert also works without one", False),
)


def check(production: bool = False, environ: dict[str, str] | None = None) -> tuple[list[str], list[str]]:
    """Return (report lines, problems). No value of any variable appears in either."""
    env = os.environ if environ is None else environ
    lines, problems = [], []

    version = sys.version_info
    lines.append(f"Python: {version.major}.{version.minor}.{version.micro}")
    if version[:2] < MINIMUM_PYTHON:
        problems.append(f"Python {MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]} or newer is required.")

    dotenv = PROJECT_ROOT / ".env"
    lines.append(f".env file: {'present (local run)' if dotenv.is_file() else 'absent (settings come from the environment)'}")

    backend = (env.get("DATABASE_BACKEND") or "sqlite").strip().lower()
    known = backend in ("sqlite", "postgres", "postgresql")
    lines.append(f"Database backend: {backend if known else 'unrecognised value'}")
    if not known:
        problems.append("DATABASE_BACKEND must be sqlite or postgres.")
    elif production and backend == "sqlite":
        problems.append("DATABASE_BACKEND must be postgres in production; a runner's local SQLite file is thrown away.")
    if backend in ("postgres", "postgresql") and not (env.get("DATABASE_URL") or "").strip():
        problems.append("DATABASE_BACKEND is postgres but DATABASE_URL is MISSING.")

    lines.append("Environment variables:")
    for name, purpose, required in VARIABLES:
        present = bool((env.get(name) or "").strip())
        need = "required" if required else "optional"
        lines.append(f"  {name:<30} {'PRESENT' if present else 'MISSING':<8} ({need}: {purpose})")
        if production and required and not present and f"{name} is MISSING" not in " ".join(problems):
            problems.append(f"{name} is MISSING.")
    return lines, problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.preflight", description=__doc__.splitlines()[0])
    parser.add_argument("--production", action="store_true",
                        help="fail unless the backend is postgres and every required setting is present")
    args = parser.parse_args(argv)

    try:
        Settings.from_env()  # loads .env if there is one, exactly as the application does
    except ValueError as exc:
        print(f"CONFIGURATION ERROR: {exc}", file=sys.stderr)
        return 2

    lines, problems = check(production=args.production)
    print("PREFLIGHT" + (" (production)" if args.production else ""))
    print("\n".join(lines))
    if problems:
        print("\nNOT READY:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("\nOK: nothing was connected to and nothing was sent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
