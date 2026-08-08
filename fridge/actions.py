"""Execute a :class:`ParsedAction` against the database and build a reply.

Kept free of any Telegram imports so the full "message -> effect -> reply"
path can be unit-tested with an in-memory SQLite connection.

Items live in a *scope* (a user's personal fridge in DMs, or a group's shared
fridge in group chats) and are attributed to whoever added them. Buyer names
are shown only for explicit attribution queries (``who_has`` / ``by_user``),
not on ordinary list/have/expiring replies.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from . import db
from .models import (
    ACTION_ADD,
    ACTION_QUERY,
    ACTION_REMOVE,
    ACTION_UPDATE,
    QUERY_BY_USER,
    QUERY_EXPIRING,
    QUERY_HAVE_ITEM,
    QUERY_LIST_ALL,
    QUERY_WHO_HAS,
    InventoryItem,
    ParsedAction,
)


def _fmt_qty(qty: float) -> str:
    """Render 2.0 as "2" but keep 1.5 as "1.5"."""
    if qty == int(qty):
        return str(int(qty))
    return f"{qty:g}"


def _fmt_item(item: InventoryItem, *, with_expiry: bool = True) -> str:
    parts = [f"{_fmt_qty(item.item_qty)}"]
    if item.unit:
        parts.append(item.unit)
    parts.append(item.item_name)
    line = " ".join(parts)
    if with_expiry and item.expires_on:
        line += f" (expires {item.expires_on})"
    return line


def execute(
    conn: sqlite3.Connection,
    scope_key: str,
    action: ParsedAction,
    added_by: Optional[str] = None,
    is_group: bool = False,
) -> str:
    """Apply ``action`` in ``scope_key`` and return a plain-language reply.

    ``added_by`` attributes writes to a specific user (defaults to the scope for
    personal fridges). ``is_group`` only changes list wording ("the group has"
    vs "you have"); buyer names appear only on attribution queries.
    """
    if action.action == ACTION_ADD:
        return _do_add(conn, scope_key, action, added_by)
    if action.action == ACTION_REMOVE:
        return _do_remove(conn, scope_key, action, added_by)
    if action.action == ACTION_UPDATE:
        return _do_update(conn, scope_key, action, added_by)
    if action.action == ACTION_QUERY:
        return _do_query(conn, scope_key, action, is_group)
    return (
        "Sorry, I didn't quite catch that. Try things like "
        '"bought 2 cartons of milk", "used the last of the cheese", '
        'or "what\'s expiring soon?"'
    )


def _do_add(
    conn: sqlite3.Connection,
    scope_key: str,
    action: ParsedAction,
    added_by: Optional[str],
) -> str:
    if not action.items:
        return "I couldn't tell what you added. Try \"bought milk and eggs\"."
    lines = []
    for spec in action.items:
        item, created = db.add_or_increment(
            conn,
            scope_key,
            item_name=spec.item_name,
            item_qty=spec.item_qty,
            unit=spec.unit,
            expires_on=spec.expires_on,
            category=spec.category,
            added_by=added_by,
        )
        verb = "Added" if created else "Updated"
        lines.append(f"{verb} {_fmt_item(item)}.")
    return "\n".join(lines)


def _do_remove(
    conn: sqlite3.Connection,
    scope_key: str,
    action: ParsedAction,
    added_by: Optional[str],
) -> str:
    if not action.items:
        return "I couldn't tell what to remove. Try \"used the last of the milk\"."
    lines = []
    for spec in action.items:
        status, item = db.remove_quantity(
            conn,
            scope_key,
            item_name=spec.item_name,
            item_qty=spec.item_qty,
            remove_all=spec.remove_all,
            prefer_user=added_by,
        )
        if status == "not_found":
            lines.append(f"There's no {spec.item_name} on record.")
        elif status == "deleted":
            lines.append(f"Removed all {spec.item_name}.")
        else:  # reduced
            assert item is not None
            lines.append(f"Updated {spec.item_name}: {_fmt_qty(item.item_qty)} left.")
    return "\n".join(lines)


def _do_update(
    conn: sqlite3.Connection,
    scope_key: str,
    action: ParsedAction,
    added_by: Optional[str],
) -> str:
    if not action.items:
        return "I couldn't tell what to update."
    lines = []
    for spec in action.items:
        item = db.update_item(
            conn,
            scope_key,
            item_name=spec.item_name,
            item_qty=spec.item_qty,
            unit=spec.unit,
            expires_on=spec.expires_on,
            category=spec.category,
            prefer_user=added_by,
        )
        if item is None:
            # Nothing to update -> treat as an add so the user isn't stuck.
            item, _ = db.add_or_increment(
                conn,
                scope_key,
                item_name=spec.item_name,
                item_qty=spec.item_qty,
                unit=spec.unit,
                expires_on=spec.expires_on,
                category=spec.category,
                added_by=added_by,
            )
            lines.append(f"Added {_fmt_item(item)}.")
        else:
            lines.append(f"Updated {_fmt_item(item)}.")
    return "\n".join(lines)


def _do_query(
    conn: sqlite3.Connection,
    scope_key: str,
    action: ParsedAction,
    is_group: bool,
) -> str:
    query_type = action.query_type or QUERY_LIST_ALL

    if query_type == QUERY_WHO_HAS:
        target = (action.query_target or "").strip()
        if not target and action.items:
            target = action.items[0].item_name
        if not target:
            return "Which item are you asking about?"
        matches = db.search_items(conn, scope_key, target)
        if not matches:
            return f"No {target} on record here."
        listed = "\n".join(f"- {_fmt_item(m)} \u2014 @{m.user_id}" for m in matches)
        return f"Here's who bought {target}:\n{listed}"

    if query_type == QUERY_BY_USER:
        target = (action.query_target or "").strip().lstrip("@")
        if not target:
            return "Whose purchases are you asking about?"
        matches = db.get_items_by_user(conn, scope_key, target)
        if not matches:
            return f"Nothing on record from @{target}."
        listed = "\n".join(f"- {_fmt_item(m)}" for m in matches)
        return f"@{target} added:\n{listed}"

    if query_type == QUERY_HAVE_ITEM:
        target = (action.query_target or "").strip()
        if not target and action.items:
            target = action.items[0].item_name
        if not target:
            return "What item are you asking about?"
        matches = db.search_items(conn, scope_key, target)
        if not matches:
            return f"No, there's no {target} here."
        # No buyer names here — ask "who bought X?" for attribution.
        listed = "\n".join(f"- {_fmt_item(m)}" for m in matches)
        return f"Yes:\n{listed}"

    if query_type == QUERY_EXPIRING:
        # within_days is decided by the caller for the daily job; for on-demand
        # queries we use a slightly wider window so "soon" feels useful.
        items = db.get_expiring(conn, scope_key, within_days=3)
        if not items:
            return "Nothing is expiring in the next few days."
        listed = "\n".join(f"- {_fmt_item(i)}" for i in items)
        return f"Expiring soon:\n{listed}"

    # QUERY_LIST_ALL (default) — no buyer names (ask "who bought X?" for that).
    items = db.get_items(conn, scope_key)
    if not items:
        return "The fridge is empty (as far as I know!)."
    listed = "\n".join(f"- {_fmt_item(i)}" for i in items)
    header = "Here's what the group has:" if is_group else "Here's what you have:"
    return f"{header}\n{listed}"
