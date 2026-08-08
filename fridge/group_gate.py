"""When to respond in group chats (no Telegram dependency beyond duck typing).

In groups the bot should stay quiet on normal conversation. It responds when:

- someone @mentions it or replies to one of its messages, or
- the text clearly looks like a fridge/pantry intent (bought / used / list / …).

Private chats are always handled by the caller (this module only classifies).
"""

from __future__ import annotations

import re
from typing import Any, Optional

# Phrases that look like fridge commands even without an @mention.
_FRIDGE_INTENT = re.compile(
    r"(?:"
    r"\b(?:bought|buy|got|get|add|added|restock(?:ed)?|picked\s+up)\b|"
    r"\b(?:used|ate|eat|finished|threw|toss(?:ed)?|ran\s+out|remove(?:d)?)\b|"
    r"\b(?:expir\w*|inventory|fridge|pantry|recipe|recipes)\b|"
    r"\b(?:what\s+can\s+(?:i|we)\s+cook|what\s+should\s+i\s+(?:cook|make))\b|"
    r"\b(?:what\s+do\s+(?:i|we)\s+have|what\s+have\s+(?:i|we)\s+got)\b|"
    r"\b(?:do\s+(?:i|we)\s+have|who\s+bought|whose|what\s+did)\b|"
    r"\b(?:meal\s+ideas?|cook\s+with|make\s+with)\b"
    r")",
    re.IGNORECASE,
)


def looks_like_fridge_intent(text: str) -> bool:
    """True if the message likely targets the fridge bot without an @mention."""
    return bool(text and _FRIDGE_INTENT.search(text))


def strip_bot_mention(text: str, bot_username: str) -> str:
    """Remove ``@BotName`` so parsing sees a clean inventory phrase."""
    if not text or not bot_username:
        return text or ""
    cleaned = re.sub(
        rf"@{re.escape(bot_username)}\b", "", text, flags=re.IGNORECASE
    )
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def is_addressed_to_bot(message: Any, bot_id: int, bot_username: str) -> bool:
    """True if the message is a reply to the bot or @mentions it."""
    if message is None:
        return False

    reply = getattr(message, "reply_to_message", None)
    if reply is not None:
        from_user = getattr(reply, "from_user", None)
        if from_user is not None and getattr(from_user, "id", None) == bot_id:
            return True

    uname = (bot_username or "").lower().lstrip("@")
    text = (getattr(message, "text", None) or "") + "\n" + (
        getattr(message, "caption", None) or ""
    )
    entities = list(getattr(message, "entities", None) or []) + list(
        getattr(message, "caption_entities", None) or []
    )
    for ent in entities:
        ent_type = getattr(ent, "type", None)
        if ent_type == "mention":
            start = getattr(ent, "offset", 0)
            length = getattr(ent, "length", 0)
            mention = text[start : start + length].lower()
            if mention == f"@{uname}":
                return True
        if ent_type == "text_mention":
            user = getattr(ent, "user", None)
            if user is not None and getattr(user, "id", None) == bot_id:
                return True

    return f"@{uname}" in text.lower() if uname else False


def should_handle_in_group(
    *,
    is_group: bool,
    message: Any,
    bot_id: int,
    bot_username: str,
    text: Optional[str] = None,
    allow_fridge_intent: bool = True,
) -> bool:
    """Decide whether a group update should be processed.

    Private chats (``is_group=False``) always return True.
    """
    if not is_group:
        return True
    if is_addressed_to_bot(message, bot_id, bot_username):
        return True
    if allow_fridge_intent and looks_like_fridge_intent(text or ""):
        return True
    return False
