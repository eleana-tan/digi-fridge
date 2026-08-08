"""Plain dataclasses shared across layers.

These are deliberately dependency-free so both the parser and the database
layers can use them without importing each other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Recognised structured actions produced by the parser.
ACTION_ADD = "add"
ACTION_REMOVE = "remove"
ACTION_UPDATE = "update"
ACTION_QUERY = "query"
ACTION_UNKNOWN = "unknown"

# Recognised query sub-types.
QUERY_LIST_ALL = "list_all"
QUERY_EXPIRING = "expiring_soon"
QUERY_HAVE_ITEM = "have_item"
QUERY_WHO_HAS = "who_has"  # "who bought the milk?" (item -> buyers)
QUERY_BY_USER = "by_user"  # "what did Alice buy?" (user -> their items)
QUERY_RECIPES = "recipes"  # "what can I cook?" / recipe inspiration


@dataclass
class ItemSpec:
    """One item referenced by a parsed message.

    Fields are optional because a message may only specify some of them
    (e.g. "used the last of the cheese" only knows the name).
    """

    item_name: str
    item_qty: Optional[float] = None
    unit: Optional[str] = None
    expires_on: Optional[str] = None  # ISO date string "YYYY-MM-DD"
    category: Optional[str] = None
    remove_all: bool = False  # e.g. "used the last of ..."


@dataclass
class ParsedAction:
    """Structured result of parsing a freeform message."""

    action: str  # one of ACTION_*
    items: list[ItemSpec] = field(default_factory=list)
    query_type: Optional[str] = None  # one of QUERY_* when action == query
    query_target: Optional[str] = None  # item name for QUERY_HAVE_ITEM
    # Human-readable explanation of what the parser thought the user meant.
    # Handy for debugging misparses via the action log.
    notes: Optional[str] = None


@dataclass
class InventoryItem:
    """A row in the ``inventory`` table."""

    id: int
    user_id: str
    item_name: str
    item_qty: float
    unit: Optional[str]
    created_ts: str
    updated_ts: str
    expires_on: Optional[str]
    category: Optional[str]
