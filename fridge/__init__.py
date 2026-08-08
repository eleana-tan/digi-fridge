"""Fridge/pantry inventory Telegram bot.

Package layout (kept intentionally boring and decoupled):

- ``config``     : environment / settings loading (no external deps)
- ``models``     : plain dataclasses shared across layers
- ``db``         : SQLite storage layer + migrations + CRUD + action log
- ``parser``     : freeform message -> structured action (LLM or rule-based)
- ``transcribe`` : voice audio -> text
- ``vision``     : grocery/receipt photo -> proposed items
- ``recipes``    : ingredients -> recipe ideas + clickable links
- ``pending``    : draft edit/confirm helpers for photo proposals
- ``actions``    : execute a parsed action against the DB, build a reply
- ``reminders``  : find + notify about soon-to-expire items
- ``bot``        : Telegram wiring / entrypoint (the only Telegram-aware module)
- ``cli``        : offline pipeline tester

The ``parser``, ``vision``, ``recipes``, and ``pending`` layers never import
Telegram, so they can be unit-tested without a live bot connection.
"""

__version__ = "0.1.0"
