"""Pending photo-proposal edits (no Telegram dependency).

After a receipt/grocery photo is extracted, the bot holds a draft list of
:class:`ItemSpec` until the user confirms. Edits are applied here so the logic
can be unit-tested without a live Telegram connection.

Supported edit phrases (case-insensitive):

- confirm: ``yes``, ``confirm``, ``add``, ``add all``, ``ok``, ``okay``
- cancel:  ``no``, ``cancel``, ``nevermind``, ``never mind``
- remove by index: ``remove 2``, ``drop 2``, ``delete 2``
- remove by name:  ``remove milk``, ``drop the milk``
- change line:     ``change 1 to 2 cartons milk``, ``set 1 to 3 eggs``
"""

from __future__ import annotations

import re
from typing import Optional

from .models import ItemSpec
from .parser import RuleBasedParser

# Outcomes of :func:`apply_pending_edit`.
STATUS_UPDATED = "updated"
STATUS_CONFIRMED = "confirmed"
STATUS_CANCELLED = "cancelled"
STATUS_NOT_AN_EDIT = "not_an_edit"
STATUS_ERROR = "error"


def format_pending(items: list[ItemSpec]) -> str:
    """Numbered draft list shown to the user before save."""
    lines = []
    for i, spec in enumerate(items, 1):
        qty = ""
        if spec.item_qty is not None:
            qty_val = (
                str(int(spec.item_qty))
                if spec.item_qty == int(spec.item_qty)
                else f"{spec.item_qty:g}"
            )
            qty = f"{qty_val} "
        unit = f"{spec.unit} " if spec.unit else ""
        extra = f" (expires {spec.expires_on})" if spec.expires_on else ""
        lines.append(f"{i}. {qty}{unit}{spec.item_name}{extra}")
    return "\n".join(lines) if lines else "(no items)"


def edit_help_text() -> str:
    return (
        "Tap \u2705 Add all or \u274c Cancel, or edit first:\n"
        "\u2022 remove 2\n"
        "\u2022 change 1 to 2 cartons milk\n"
        "\u2022 yes / no"
    )


def apply_pending_edit(
    items: list[ItemSpec], message: str
) -> tuple[str, list[ItemSpec], str]:
    """Apply a user message to a pending draft.

    Returns ``(status, new_items, reply_fragment)`` where status is one of the
    ``STATUS_*`` constants. ``STATUS_NOT_AN_EDIT`` means the message should be
    handled by the normal inventory pipeline instead.
    """
    text = (message or "").strip()
    lower = text.lower()
    if not text:
        return STATUS_NOT_AN_EDIT, items, ""

    if lower in {"yes", "y", "confirm", "add", "add all", "ok", "okay", "done"}:
        if not items:
            return STATUS_ERROR, items, "There's nothing left to add."
        return STATUS_CONFIRMED, items, ""

    if lower in {"no", "n", "cancel", "nevermind", "never mind", "stop"}:
        return STATUS_CANCELLED, [], ""

    remove_idx = re.fullmatch(r"(?:remove|drop|delete|rm)\s+(\d+)\s*\.?", lower)
    if remove_idx:
        idx = int(remove_idx.group(1))
        if idx < 1 or idx > len(items):
            return STATUS_ERROR, items, f"There's no item #{idx}."
        removed = items[idx - 1]
        new_items = items[: idx - 1] + items[idx:]
        return (
            STATUS_UPDATED,
            new_items,
            f"Removed #{idx} ({removed.item_name}).",
        )

    remove_name = re.fullmatch(
        r"(?:remove|drop|delete|rm)\s+(?:the\s+|item\s+)?(.+)", lower
    )
    if remove_name:
        name = remove_name.group(1).strip().rstrip(".")
        for i, spec in enumerate(items):
            if spec.item_name.lower() == name or name in spec.item_name.lower():
                new_items = items[:i] + items[i + 1 :]
                return (
                    STATUS_UPDATED,
                    new_items,
                    f"Removed {spec.item_name}.",
                )
        return STATUS_ERROR, items, f'I don\'t see "{name}" in the draft.'

    change = re.fullmatch(
        r"(?:change|set|update)\s+(\d+)\s+(?:to\s+|as\s+)?(.+)", lower, re.DOTALL
    )
    if change:
        idx = int(change.group(1))
        if idx < 1 or idx > len(items):
            return STATUS_ERROR, items, f"There's no item #{idx}."
        replacement = _parse_replacement(change.group(2).strip())
        if replacement is None:
            return (
                STATUS_ERROR,
                items,
                'Couldn\'t parse that. Try: change 1 to 2 cartons milk',
            )
        new_items = list(items)
        new_items[idx - 1] = replacement
        return (
            STATUS_UPDATED,
            new_items,
            f"Updated #{idx} to {format_pending([replacement]).removeprefix('1. ')}.",
        )

    return STATUS_NOT_AN_EDIT, items, ""


def _parse_replacement(chunk: str) -> Optional[ItemSpec]:
    """Turn ``2 cartons milk`` into an ItemSpec via the rule-based parser."""
    # Prefix with a buy-verb so the rule parser treats it as an add-item phrase.
    parsed = RuleBasedParser().parse(f"bought {chunk}")
    if parsed.items:
        return parsed.items[0]
    # Bare name fallback.
    name = chunk.strip()
    if not name:
        return None
    return ItemSpec(item_name=name)

