"""Offline CLI to exercise the parse -> DB -> reply pipeline without Telegram.

Useful for verifying steps 2-6 with no bot token. It uses the same parser the
bot would (LLM if OPENAI_API_KEY is set, otherwise the rule-based parser) and a
real SQLite database file.

Examples::

    python -m fridge.cli "bought 2 cartons of milk and a dozen eggs"
    python -m fridge.cli "what do I have?"
    python -m fridge.cli --user alice "used the last of the cheese"

Start an interactive session with no message argument::

    python -m fridge.cli
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict

from . import actions, db
from .config import get_settings
from .parser import build_parser


def run_once(conn, parser, user_id: str, message: str, *, show_parse: bool) -> str:
    parsed = parser.parse(message)
    db.log_action(conn, user_id, message, json.dumps(asdict(parsed)))
    if show_parse:
        print("  parsed:", json.dumps(asdict(parsed), indent=2))
    # Behave like a personal DM fridge for the CLI user.
    scope_key = f"user:{user_id}"
    return actions.execute(conn, scope_key, parsed, added_by=user_id, is_group=False)


def main() -> None:
    settings = get_settings()
    ap = argparse.ArgumentParser(description="Offline fridge-bot pipeline tester")
    ap.add_argument("message", nargs="*", help="message to send (omit for interactive)")
    ap.add_argument("--user", default="cli_user", help="user_id to act as")
    ap.add_argument("--db", default=settings.db_path, help="SQLite path")
    ap.add_argument("--show-parse", action="store_true", help="print parsed action")
    args = ap.parse_args()

    conn = db.connect(args.db)
    parser = build_parser(
        openai_api_key=settings.openai_api_key,
        model=settings.openai_model,
        temperature=settings.openai_temperature,
        mode=settings.parser_mode,
        reasoning_effort=settings.openai_reasoning_effort,
        max_completion_tokens=settings.openai_max_tokens,
    )
    kind = "LLM" if settings.has_openai else "rule-based"
    print(f"[using {kind} parser, db={args.db}, user={args.user}]")

    if args.message:
        print(run_once(conn, parser, args.user, " ".join(args.message),
                       show_parse=args.show_parse))
        return

    print("Interactive mode. Type a message (or 'quit').")
    while True:
        try:
            message = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if message.lower() in {"quit", "exit"}:
            break
        if not message:
            continue
        print(run_once(conn, parser, args.user, message, show_parse=args.show_parse))


if __name__ == "__main__":
    main()
