"""SQLite storage layer: migrations, CRUD, and the action log.

This module knows nothing about Telegram or the LLM. It works on a plain
``sqlite3.Connection`` so it can be unit-tested against an in-memory database.

Timestamps are stored as ISO-8601 UTC strings; ``expires_on`` is an ISO date
string ("YYYY-MM-DD") or NULL.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional

from .models import InventoryItem

# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------
# Each migration is (version, sql). They run in order; applied versions are
# recorded in ``schema_migrations`` so re-running is a no-op.
_MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS inventory (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     TEXT    NOT NULL,
            item_name   TEXT    NOT NULL,
            item_qty    REAL    NOT NULL DEFAULT 1,
            unit        TEXT,
            created_ts  TEXT    NOT NULL,
            updated_ts  TEXT    NOT NULL,
            expires_on  TEXT,
            category    TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_inventory_user
            ON inventory (user_id);
        CREATE INDEX IF NOT EXISTS idx_inventory_user_name
            ON inventory (user_id, item_name);

        -- Lightweight debug log of every message + how it was parsed.
        CREATE TABLE IF NOT EXISTS action_log (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id        TEXT NOT NULL,
            raw_message    TEXT NOT NULL,
            parsed_action  TEXT,
            timestamp      TEXT NOT NULL
        );

        -- Maps a user handle to the Telegram chat id so background jobs
        -- (expiry reminders) can proactively message the user.
        CREATE TABLE IF NOT EXISTS users (
            user_id    TEXT PRIMARY KEY,
            chat_id    INTEGER NOT NULL,
            updated_ts TEXT NOT NULL
        );
        """,
    ),
]


def utcnow_iso() -> str:
    """Return the current UTC time as an ISO-8601 string (seconds precision)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(db_path: str) -> sqlite3.Connection:
    """Open a connection with sensible defaults and run migrations."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """Apply any pending migrations."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    INTEGER PRIMARY KEY,
            applied_ts TEXT NOT NULL
        );
        """
    )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    for version, sql in _MIGRATIONS:
        if version in applied:
            continue
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_ts) VALUES (?, ?)",
            (version, utcnow_iso()),
        )
    conn.commit()


def _row_to_item(row: sqlite3.Row) -> InventoryItem:
    return InventoryItem(
        id=row["id"],
        user_id=row["user_id"],
        item_name=row["item_name"],
        item_qty=row["item_qty"],
        unit=row["unit"],
        created_ts=row["created_ts"],
        updated_ts=row["updated_ts"],
        expires_on=row["expires_on"],
        category=row["category"],
    )


# ---------------------------------------------------------------------------
# User <-> chat mapping
# ---------------------------------------------------------------------------
def remember_user(conn: sqlite3.Connection, user_id: str, chat_id: int) -> None:
    """Upsert the handle -> chat id mapping used for reminders."""
    now = utcnow_iso()
    conn.execute(
        """
        INSERT INTO users (user_id, chat_id, updated_ts)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            chat_id = excluded.chat_id,
            updated_ts = excluded.updated_ts
        """,
        (user_id, chat_id, now),
    )
    conn.commit()


def get_chat_id(conn: sqlite3.Connection, user_id: str) -> Optional[int]:
    row = conn.execute(
        "SELECT chat_id FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    return row["chat_id"] if row else None


# ---------------------------------------------------------------------------
# Inventory reads
# ---------------------------------------------------------------------------
def get_items(conn: sqlite3.Connection, user_id: str) -> list[InventoryItem]:
    rows = conn.execute(
        """
        SELECT * FROM inventory
        WHERE user_id = ?
        ORDER BY (expires_on IS NULL), expires_on, item_name
        """,
        (user_id,),
    ).fetchall()
    return [_row_to_item(r) for r in rows]


def find_item(
    conn: sqlite3.Connection, user_id: str, item_name: str
) -> Optional[InventoryItem]:
    """Case-insensitive lookup of a single item by name."""
    row = conn.execute(
        """
        SELECT * FROM inventory
        WHERE user_id = ? AND LOWER(item_name) = LOWER(?)
        ORDER BY id
        LIMIT 1
        """,
        (user_id, item_name.strip()),
    ).fetchone()
    return _row_to_item(row) if row else None


def search_items(
    conn: sqlite3.Connection, user_id: str, term: str
) -> list[InventoryItem]:
    """Fuzzy-ish search: items whose name contains ``term``."""
    rows = conn.execute(
        """
        SELECT * FROM inventory
        WHERE user_id = ? AND LOWER(item_name) LIKE LOWER(?)
        ORDER BY item_name
        """,
        (user_id, f"%{term.strip()}%"),
    ).fetchall()
    return [_row_to_item(r) for r in rows]


def get_expiring(
    conn: sqlite3.Connection, user_id: str, within_days: int, today: Optional[str] = None
) -> list[InventoryItem]:
    """Items with an ``expires_on`` on or before today + ``within_days``.

    ``today`` may be supplied (ISO date) for deterministic testing.
    """
    if today is None:
        today = datetime.now(timezone.utc).date().isoformat()
    rows = conn.execute(
        """
        SELECT * FROM inventory
        WHERE user_id = ?
          AND expires_on IS NOT NULL
          AND date(expires_on) <= date(?, '+' || ? || ' days')
        ORDER BY expires_on
        """,
        (user_id, today, within_days),
    ).fetchall()
    return [_row_to_item(r) for r in rows]


def users_with_expiring(
    conn: sqlite3.Connection, within_days: int, today: Optional[str] = None
) -> list[str]:
    """Distinct user_ids that have at least one soon-to-expire item."""
    if today is None:
        today = datetime.now(timezone.utc).date().isoformat()
    rows = conn.execute(
        """
        SELECT DISTINCT user_id FROM inventory
        WHERE expires_on IS NOT NULL
          AND date(expires_on) <= date(?, '+' || ? || ' days')
        """,
        (today, within_days),
    ).fetchall()
    return [r["user_id"] for r in rows]


# ---------------------------------------------------------------------------
# Inventory writes
# ---------------------------------------------------------------------------
def add_or_increment(
    conn: sqlite3.Connection,
    user_id: str,
    item_name: str,
    item_qty: Optional[float] = None,
    unit: Optional[str] = None,
    expires_on: Optional[str] = None,
    category: Optional[str] = None,
) -> tuple[InventoryItem, bool]:
    """Add a new item, or increment the quantity if it already exists.

    Returns ``(item, created)`` where ``created`` is True for a fresh row.
    Non-null ``unit``/``expires_on``/``category`` overwrite existing values.
    """
    qty = 1.0 if item_qty is None else float(item_qty)
    now = utcnow_iso()
    existing = find_item(conn, user_id, item_name)
    if existing is None:
        cur = conn.execute(
            """
            INSERT INTO inventory
                (user_id, item_name, item_qty, unit, created_ts, updated_ts,
                 expires_on, category)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, item_name.strip(), qty, unit, now, now, expires_on, category),
        )
        conn.commit()
        created_row = conn.execute(
            "SELECT * FROM inventory WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return _row_to_item(created_row), True

    new_qty = existing.item_qty + qty
    conn.execute(
        """
        UPDATE inventory
        SET item_qty = ?,
            unit = COALESCE(?, unit),
            expires_on = COALESCE(?, expires_on),
            category = COALESCE(?, category),
            updated_ts = ?
        WHERE id = ?
        """,
        (new_qty, unit, expires_on, category, now, existing.id),
    )
    conn.commit()
    updated = conn.execute(
        "SELECT * FROM inventory WHERE id = ?", (existing.id,)
    ).fetchone()
    return _row_to_item(updated), False


def remove_quantity(
    conn: sqlite3.Connection,
    user_id: str,
    item_name: str,
    item_qty: Optional[float] = None,
    remove_all: bool = False,
) -> tuple[str, Optional[InventoryItem]]:
    """Reduce quantity or delete an item.

    Returns ``(status, item_or_none)`` where status is one of:
    ``"not_found"``, ``"deleted"``, or ``"reduced"``.
    """
    existing = find_item(conn, user_id, item_name)
    if existing is None:
        return "not_found", None

    now = utcnow_iso()
    if remove_all or item_qty is None:
        # No explicit quantity -> treat as "use it up" and delete.
        conn.execute("DELETE FROM inventory WHERE id = ?", (existing.id,))
        conn.commit()
        return "deleted", existing

    new_qty = existing.item_qty - float(item_qty)
    if new_qty <= 0:
        conn.execute("DELETE FROM inventory WHERE id = ?", (existing.id,))
        conn.commit()
        return "deleted", existing

    conn.execute(
        "UPDATE inventory SET item_qty = ?, updated_ts = ? WHERE id = ?",
        (new_qty, now, existing.id),
    )
    conn.commit()
    updated = conn.execute(
        "SELECT * FROM inventory WHERE id = ?", (existing.id,)
    ).fetchone()
    return "reduced", _row_to_item(updated)


def update_item(
    conn: sqlite3.Connection,
    user_id: str,
    item_name: str,
    item_qty: Optional[float] = None,
    unit: Optional[str] = None,
    expires_on: Optional[str] = None,
    category: Optional[str] = None,
) -> Optional[InventoryItem]:
    """Overwrite specified fields of an existing item. Non-null values win.

    Returns the updated item, or None if it doesn't exist.
    """
    existing = find_item(conn, user_id, item_name)
    if existing is None:
        return None
    now = utcnow_iso()
    conn.execute(
        """
        UPDATE inventory
        SET item_qty = COALESCE(?, item_qty),
            unit = COALESCE(?, unit),
            expires_on = COALESCE(?, expires_on),
            category = COALESCE(?, category),
            updated_ts = ?
        WHERE id = ?
        """,
        (item_qty, unit, expires_on, category, now, existing.id),
    )
    conn.commit()
    updated = conn.execute(
        "SELECT * FROM inventory WHERE id = ?", (existing.id,)
    ).fetchone()
    return _row_to_item(updated)


def delete_item(conn: sqlite3.Connection, user_id: str, item_name: str) -> bool:
    existing = find_item(conn, user_id, item_name)
    if existing is None:
        return False
    conn.execute("DELETE FROM inventory WHERE id = ?", (existing.id,))
    conn.commit()
    return True


# ---------------------------------------------------------------------------
# Action log
# ---------------------------------------------------------------------------
def log_action(
    conn: sqlite3.Connection,
    user_id: str,
    raw_message: str,
    parsed_action: Optional[str],
) -> None:
    conn.execute(
        """
        INSERT INTO action_log (user_id, raw_message, parsed_action, timestamp)
        VALUES (?, ?, ?, ?)
        """,
        (user_id, raw_message, parsed_action, utcnow_iso()),
    )
    conn.commit()
