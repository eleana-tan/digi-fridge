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
    user_id: str
    chat_id: int
    text: str


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
    """Return one :class:`Reminder` per user who has soon-to-expire items.

    Users without a known chat id (never messaged the bot) are skipped, since
    there's no way to reach them.
    """
    reminders: list[Reminder] = []
    for user_id in db.users_with_expiring(conn, within_days, today=today):
        chat_id = db.get_chat_id(conn, user_id)
        if chat_id is None:
            continue
        items = db.get_expiring(conn, user_id, within_days, today=today)
        if not items:
            continue
        reminders.append(
            Reminder(
                user_id=user_id,
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
