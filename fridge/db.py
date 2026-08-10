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

from .models import InventoryItem, SavedRecipe

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
    (
        2,
        # Introduce a "scope" so a group chat can be one shared fridge while each
        # item keeps its attribution in ``user_id`` (who added it). In DMs the
        # scope is ``user:<handle>``; in groups it's ``chat:<group_id>``.
        # Existing rows are backfilled to their owner's personal scope.
        """
        ALTER TABLE inventory ADD COLUMN scope_key TEXT;
        UPDATE inventory SET scope_key = 'user:' || user_id WHERE scope_key IS NULL;
        CREATE INDEX IF NOT EXISTS idx_inventory_scope
            ON inventory (scope_key);
        CREATE INDEX IF NOT EXISTS idx_inventory_scope_name
            ON inventory (scope_key, item_name);
        """,
    ),
    (
        3,
        """
        CREATE TABLE IF NOT EXISTS saved_recipes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            scope_key   TEXT NOT NULL,
            added_by    TEXT NOT NULL,
            url         TEXT NOT NULL,
            title       TEXT NOT NULL,
            keywords    TEXT NOT NULL DEFAULT '',
            created_ts  TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_saved_recipes_scope
            ON saved_recipes (scope_key);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_saved_recipes_scope_url
            ON saved_recipes (scope_key, url);
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
def get_items(conn: sqlite3.Connection, scope_key: str) -> list[InventoryItem]:
    """All items in a scope (a user's DM fridge or a group's shared fridge)."""
    rows = conn.execute(
        """
        SELECT * FROM inventory
        WHERE scope_key = ?
        ORDER BY (expires_on IS NULL), expires_on, item_name, user_id
        """,
        (scope_key,),
    ).fetchall()
    return [_row_to_item(r) for r in rows]


def find_item(
    conn: sqlite3.Connection,
    scope_key: str,
    item_name: str,
    prefer_user: Optional[str] = None,
) -> Optional[InventoryItem]:
    """Case-insensitive lookup of a single item by name within a scope.

    When several people in a group have the same item, ``prefer_user`` selects
    that person's row first (used so "I used the milk" affects your own).
    """
    rows = conn.execute(
        """
        SELECT * FROM inventory
        WHERE scope_key = ? AND LOWER(item_name) = LOWER(?)
        ORDER BY id
        """,
        (scope_key, item_name.strip()),
    ).fetchall()
    if not rows:
        return None
    if prefer_user is not None:
        for row in rows:
            if row["user_id"] == prefer_user:
                return _row_to_item(row)
    return _row_to_item(rows[0])


def search_items(
    conn: sqlite3.Connection, scope_key: str, term: str
) -> list[InventoryItem]:
    """Fuzzy-ish search within a scope: items whose name contains ``term``."""
    rows = conn.execute(
        """
        SELECT * FROM inventory
        WHERE scope_key = ? AND LOWER(item_name) LIKE LOWER(?)
        ORDER BY item_name, user_id
        """,
        (scope_key, f"%{term.strip()}%"),
    ).fetchall()
    return [_row_to_item(r) for r in rows]


def get_items_by_user(
    conn: sqlite3.Connection, scope_key: str, user_id: str
) -> list[InventoryItem]:
    """Items in a scope attributed to a specific contributor."""
    rows = conn.execute(
        """
        SELECT * FROM inventory
        WHERE scope_key = ? AND LOWER(user_id) = LOWER(?)
        ORDER BY (expires_on IS NULL), expires_on, item_name
        """,
        (scope_key, user_id.strip().lstrip("@")),
    ).fetchall()
    return [_row_to_item(r) for r in rows]


def get_expiring(
    conn: sqlite3.Connection, scope_key: str, within_days: int, today: Optional[str] = None
) -> list[InventoryItem]:
    """Items in a scope with ``expires_on`` on or before today + ``within_days``.

    ``today`` may be supplied (ISO date) for deterministic testing.
    """
    if today is None:
        today = datetime.now(timezone.utc).date().isoformat()
    rows = conn.execute(
        """
        SELECT * FROM inventory
        WHERE scope_key = ?
          AND expires_on IS NOT NULL
          AND date(expires_on) <= date(?, '+' || ? || ' days')
        ORDER BY expires_on
        """,
        (scope_key, today, within_days),
    ).fetchall()
    return [_row_to_item(r) for r in rows]


def scopes_with_expiring(
    conn: sqlite3.Connection, within_days: int, today: Optional[str] = None
) -> list[str]:
    """Distinct scopes that have at least one soon-to-expire item."""
    if today is None:
        today = datetime.now(timezone.utc).date().isoformat()
    rows = conn.execute(
        """
        SELECT DISTINCT scope_key FROM inventory
        WHERE expires_on IS NOT NULL
          AND date(expires_on) <= date(?, '+' || ? || ' days')
        """,
        (today, within_days),
    ).fetchall()
    return [r["scope_key"] for r in rows]


# ---------------------------------------------------------------------------
# Inventory writes
# ---------------------------------------------------------------------------
def _find_own_item(
    conn: sqlite3.Connection, scope_key: str, item_name: str, added_by: str
) -> Optional[InventoryItem]:
    """Find the row in a scope for a specific contributor (attribution match)."""
    row = conn.execute(
        """
        SELECT * FROM inventory
        WHERE scope_key = ? AND user_id = ? AND LOWER(item_name) = LOWER(?)
        ORDER BY id
        LIMIT 1
        """,
        (scope_key, added_by, item_name.strip()),
    ).fetchone()
    return _row_to_item(row) if row else None


def add_or_increment(
    conn: sqlite3.Connection,
    scope_key: str,
    item_name: str,
    item_qty: Optional[float] = None,
    unit: Optional[str] = None,
    expires_on: Optional[str] = None,
    category: Optional[str] = None,
    added_by: Optional[str] = None,
) -> tuple[InventoryItem, bool]:
    """Add a new item, or increment the contributor's existing quantity.

    Items are attributed to ``added_by`` (defaults to the scope for personal
    fridges). In a group, two people adding "milk" keep separate attributed
    rows. Returns ``(item, created)``; non-null unit/expires_on/category
    overwrite existing values.
    """
    if added_by is None:
        added_by = scope_key
    qty = 1.0 if item_qty is None else float(item_qty)
    now = utcnow_iso()
    existing = _find_own_item(conn, scope_key, item_name, added_by)
    if existing is None:
        cur = conn.execute(
            """
            INSERT INTO inventory
                (user_id, scope_key, item_name, item_qty, unit, created_ts,
                 updated_ts, expires_on, category)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                added_by,
                scope_key,
                item_name.strip(),
                qty,
                unit,
                now,
                now,
                expires_on,
                category,
            ),
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
    scope_key: str,
    item_name: str,
    item_qty: Optional[float] = None,
    remove_all: bool = False,
    prefer_user: Optional[str] = None,
) -> tuple[str, Optional[InventoryItem]]:
    """Reduce quantity or delete an item within a scope.

    In a shared group fridge, ``prefer_user`` targets the caller's own item
    first when multiple people have the same item. Returns ``(status, item)``
    where status is ``"not_found"``, ``"deleted"``, or ``"reduced"``.
    """
    existing = find_item(conn, scope_key, item_name, prefer_user=prefer_user)
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
    scope_key: str,
    item_name: str,
    item_qty: Optional[float] = None,
    unit: Optional[str] = None,
    expires_on: Optional[str] = None,
    category: Optional[str] = None,
    prefer_user: Optional[str] = None,
) -> Optional[InventoryItem]:
    """Overwrite specified fields of an existing item. Non-null values win.

    Returns the updated item, or None if it doesn't exist.
    """
    existing = find_item(conn, scope_key, item_name, prefer_user=prefer_user)
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


def delete_item(conn: sqlite3.Connection, scope_key: str, item_name: str) -> bool:
    existing = find_item(conn, scope_key, item_name)
    if existing is None:
        return False
    conn.execute("DELETE FROM inventory WHERE id = ?", (existing.id,))
    conn.commit()
    return True


def clear_scope(conn: sqlite3.Connection, scope_key: str) -> int:
    """Delete every inventory row in a scope. Returns how many rows were removed."""
    cur = conn.execute("DELETE FROM inventory WHERE scope_key = ?", (scope_key,))
    conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------------------
# Saved recipes (Instagram reels, etc.)
# ---------------------------------------------------------------------------
def _row_to_saved_recipe(row: sqlite3.Row) -> SavedRecipe:
    raw = (row["keywords"] or "").strip()
    keywords = [k for k in raw.split(",") if k] if raw else []
    return SavedRecipe(
        id=row["id"],
        scope_key=row["scope_key"],
        added_by=row["added_by"],
        url=row["url"],
        title=row["title"],
        keywords=keywords,
        created_ts=row["created_ts"],
    )


def list_saved_recipes(conn: sqlite3.Connection, scope_key: str) -> list[SavedRecipe]:
    rows = conn.execute(
        """
        SELECT * FROM saved_recipes
        WHERE scope_key = ?
        ORDER BY id DESC
        """,
        (scope_key,),
    ).fetchall()
    return [_row_to_saved_recipe(r) for r in rows]


def get_saved_recipe(
    conn: sqlite3.Connection, scope_key: str, recipe_id: int
) -> Optional[SavedRecipe]:
    row = conn.execute(
        """
        SELECT * FROM saved_recipes
        WHERE scope_key = ? AND id = ?
        """,
        (scope_key, recipe_id),
    ).fetchone()
    return _row_to_saved_recipe(row) if row else None


def find_saved_recipe_by_url(
    conn: sqlite3.Connection, scope_key: str, url: str
) -> Optional[SavedRecipe]:
    row = conn.execute(
        """
        SELECT * FROM saved_recipes
        WHERE scope_key = ? AND url = ?
        """,
        (scope_key, url),
    ).fetchone()
    return _row_to_saved_recipe(row) if row else None


def add_saved_recipe(
    conn: sqlite3.Connection,
    scope_key: str,
    added_by: str,
    url: str,
    title: str,
    keywords: list[str] | None = None,
) -> tuple[SavedRecipe, bool]:
    """Insert a saved recipe. Returns ``(recipe, created)``.

    If the URL already exists in this scope, updates keywords/title when provided
    and returns ``created=False``.
    """
    keywords = [k.strip().lower() for k in (keywords or []) if k and k.strip()]
    # Dedupe keywords, preserve order.
    seen: set[str] = set()
    clean_kw: list[str] = []
    for k in keywords:
        if k not in seen:
            seen.add(k)
            clean_kw.append(k)
    kw_text = ",".join(clean_kw)
    title = (title or "Saved recipe").strip() or "Saved recipe"
    url = url.strip()

    existing = find_saved_recipe_by_url(conn, scope_key, url)
    if existing is not None:
        # Merge keywords; keep newer title if it isn't the generic default.
        merged = list(existing.keywords)
        for k in clean_kw:
            if k not in merged:
                merged.append(k)
        new_title = title if title != "Saved recipe" else existing.title
        conn.execute(
            """
            UPDATE saved_recipes
            SET title = ?, keywords = ?
            WHERE id = ?
            """,
            (new_title, ",".join(merged), existing.id),
        )
        conn.commit()
        updated = get_saved_recipe(conn, scope_key, existing.id)
        assert updated is not None
        return updated, False

    cur = conn.execute(
        """
        INSERT INTO saved_recipes
            (scope_key, added_by, url, title, keywords, created_ts)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (scope_key, added_by, url, title, kw_text, utcnow_iso()),
    )
    conn.commit()
    recipe = get_saved_recipe(conn, scope_key, int(cur.lastrowid))
    assert recipe is not None
    return recipe, True


def delete_saved_recipe(
    conn: sqlite3.Connection, scope_key: str, recipe_id: int
) -> bool:
    cur = conn.execute(
        "DELETE FROM saved_recipes WHERE scope_key = ? AND id = ?",
        (scope_key, recipe_id),
    )
    conn.commit()
    return cur.rowcount > 0


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
