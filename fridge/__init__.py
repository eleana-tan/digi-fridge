"""Fridge/pantry inventory Telegram bot.

Package layout (kept intentionally boring and decoupled):

- ``config``    : environment / settings loading (no external deps)
- ``models``    : plain dataclasses shared across layers
- ``db``        : SQLite storage layer + migrations + CRUD + action log
- ``parser``    : freeform message -> structured action (LLM or rule-based)
- ``actions``   : execute a parsed action against the DB, build a reply
- ``reminders`` : find + notify about soon-to-expire items
- ``bot``       : Telegram wiring / entrypoint (the only Telegram-aware module)

The ``parser`` and ``bot`` layers never import each other, so parsing can be
unit-tested without a live Telegram connection.
"""

__version__ = "0.1.0"
