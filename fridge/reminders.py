"""Expiry reminders.

:func:`build_reminders` is a pure function (no Telegram, no I/O beyond the DB
read) that returns, per user, the chat id to message and the reminder text.
The Telegram-aware :func:`send_daily_reminders` just delivers those messages,
so the interesting logic stays unit-testable.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

from . import db
from .actions import _fmt_item


@dataclass
class Reminder:
    scope_key: str
    chat_id: int
    text: str


def _chat_id_for_scope(conn: sqlite3.Connection, scope_key: str) -> Optional[int]:
    """Resolve where to send a reminder for a given scope.

    - ``chat:<id>``  -> that group chat id (parsed directly).
    - ``user:<name>``-> the user's DM chat id (from the ``users`` table).
    - anything else  -> looked up as a bare user handle (back-compat).
    """
    if scope_key.startswith("chat:"):
        try:
            return int(scope_key[len("chat:"):])
        except ValueError:
            return None
    if scope_key.startswith("user:"):
        return db.get_chat_id(conn, scope_key[len("user:"):])
    return db.get_chat_id(conn, scope_key)


def format_reminder_text(items) -> str:
    """Build the plain-language reminder body for a list of items."""
    lines = [f"- {_fmt_item(i)}" for i in items]
    header = "Heads up! These items are expiring soon:"
    footer = "Use them up or toss them before they go bad."
    return f"{header}\n" + "\n".join(lines) + f"\n{footer}"


def build_reminders(
    conn: sqlite3.Connection,
    within_days: int,
    today: Optional[str] = None,
) -> list[Reminder]:
    """Return one :class:`Reminder` per scope that has soon-to-expire items.

    Scopes we can't reach (e.g. a user who never messaged the bot in DM) are
    skipped, since there's no chat to message.
    """
    reminders: list[Reminder] = []
    for scope_key in db.scopes_with_expiring(conn, within_days, today=today):
        chat_id = _chat_id_for_scope(conn, scope_key)
        if chat_id is None:
            continue
        items = db.get_expiring(conn, scope_key, within_days, today=today)
        if not items:
            continue
        reminders.append(
            Reminder(
                scope_key=scope_key,
                chat_id=chat_id,
                text=format_reminder_text(items),
            )
        )
    return reminders


async def send_daily_reminders(context) -> None:
    """python-telegram-bot JobQueue callback: message each affected user.

    Expects ``context.application.bot_data`` to hold ``conn`` (sqlite3
    connection) and ``expiry_reminder_days`` (int).
    """
    app = context.application
    conn: sqlite3.Connection = app.bot_data["conn"]
    within_days: int = app.bot_data["expiry_reminder_days"]
    for reminder in build_reminders(conn, within_days):
        await context.bot.send_message(chat_id=reminder.chat_id, text=reminder.text)
