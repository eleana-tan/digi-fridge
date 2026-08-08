"""Message parsing layer: freeform text -> :class:`ParsedAction`.

This module is intentionally decoupled from Telegram *and* from the network:

- :func:`parsed_action_from_dict` is a pure function that turns the JSON an LLM
  returns into a :class:`ParsedAction`. Unit-test it directly with sample dicts.
- :class:`LLMParser` takes an injectable ``client`` (anything exposing the
  OpenAI ``chat.completions.create`` interface), so tests can pass a stub that
  returns canned JSON without hitting the network.
- :class:`RuleBasedParser` is a dependency-free heuristic parser used as an
  offline fallback (no OPENAI_API_KEY) and for fast, deterministic tests.

Nothing here imports the ``bot`` module, so parsing can be exercised entirely
without a live Telegram connection.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from typing import Any, Optional, Protocol

from .models import (
    ACTION_ADD,
    ACTION_QUERY,
    ACTION_REMOVE,
    ACTION_UNKNOWN,
    ACTION_UPDATE,
    QUERY_EXPIRING,
    QUERY_HAVE_ITEM,
    QUERY_LIST_ALL,
    ItemSpec,
    ParsedAction,
)

_VALID_ACTIONS = {ACTION_ADD, ACTION_REMOVE, ACTION_UPDATE, ACTION_QUERY, ACTION_UNKNOWN}
_VALID_QUERIES = {QUERY_LIST_ALL, QUERY_EXPIRING, QUERY_HAVE_ITEM}

# Very small keyword -> category map used by the rule-based parser and as a
# reasonable default categoriser.
_CATEGORY_KEYWORDS: dict[str, str] = {
    "milk": "dairy",
    "cheese": "dairy",
    "yogurt": "dairy",
    "yoghurt": "dairy",
    "butter": "dairy",
    "cream": "dairy",
    "egg": "dairy",
    "eggs": "dairy",
    "chicken": "meat",
    "beef": "meat",
    "pork": "meat",
    "fish": "meat",
    "salmon": "meat",
    "bacon": "meat",
    "apple": "produce",
    "banana": "produce",
    "lettuce": "produce",
    "spinach": "produce",
    "tomato": "produce",
    "carrot": "produce",
    "onion": "produce",
    "potato": "produce",
    "bread": "pantry",
    "rice": "pantry",
    "pasta": "pantry",
    "flour": "pantry",
    "sugar": "pantry",
    "cereal": "pantry",
    "beans": "pantry",
}


def infer_category(item_name: str) -> Optional[str]:
    """Best-effort category from a name, or None if unknown."""
    name = item_name.lower()
    for keyword, category in _CATEGORY_KEYWORDS.items():
        if keyword in name:
            return category
    return None


class Parser(Protocol):
    """Anything that turns a message into a structured action."""

    def parse(self, message: str) -> ParsedAction:  # pragma: no cover - protocol
        ...


# ---------------------------------------------------------------------------
# Pure JSON -> ParsedAction transform (unit-testable, no network)
# ---------------------------------------------------------------------------
def _coerce_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parsed_action_from_dict(data: dict[str, Any]) -> ParsedAction:
    """Convert a parser JSON payload into a validated :class:`ParsedAction`.

    Unknown/invalid actions collapse to ``ACTION_UNKNOWN`` so callers never
    have to guess whether a field is trustworthy.
    """
    action = str(data.get("action", "")).strip().lower()
    if action not in _VALID_ACTIONS:
        action = ACTION_UNKNOWN

    items: list[ItemSpec] = []
    for raw in data.get("items", []) or []:
        if not isinstance(raw, dict):
            continue
        name = _clean_str(raw.get("item_name"))
        if not name:
            continue
        category = _clean_str(raw.get("category")) or infer_category(name)
        items.append(
            ItemSpec(
                item_name=name,
                item_qty=_coerce_float(raw.get("item_qty")),
                unit=_clean_str(raw.get("unit")),
                expires_on=_normalize_date(raw.get("expires_on")),
                category=category,
                remove_all=bool(raw.get("remove_all", False)),
            )
        )

    query_type = _clean_str(data.get("query_type"))
    if query_type is not None:
        query_type = query_type.lower()
        if query_type not in _VALID_QUERIES:
            query_type = None

    return ParsedAction(
        action=action,
        items=items,
        query_type=query_type,
        query_target=_clean_str(data.get("query_target")),
        notes=_clean_str(data.get("notes")),
    )


def _normalize_date(value: Any) -> Optional[str]:
    """Return an ISO date string if ``value`` looks like a date, else None."""
    text = _clean_str(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    # Already ISO-ish? Accept the leading date portion of a datetime.
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", text)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# LLM parser
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = """\
You convert a user's freeform message about their fridge/pantry into a single
structured JSON action. Respond with ONLY a JSON object, no prose.

Schema:
{
  "action": "add" | "remove" | "update" | "query" | "unknown",
  "query_type": "list_all" | "expiring_soon" | "have_item" | null,
  "query_target": string | null,   // item name for have_item queries
  "items": [
    {
      "item_name": string,          // singular, lowercase, no quantity words
      "item_qty": number | null,
      "unit": string | null,        // e.g. "carton", "count", "g", "loaf"
      "expires_on": string | null,  // ISO date YYYY-MM-DD, resolve relative dates
      "category": string | null,    // dairy | produce | meat | pantry | frozen | other
      "remove_all": boolean         // true for "used the last of X" / "ran out of X"
    }
  ],
  "notes": string | null            // short note on how you interpreted it
}

Rules:
- "bought", "got", "picked up", "add", "restocked" => action "add".
- "used", "ate", "finished", "threw out", "ran out of", "used the last of" => action "remove".
  Set remove_all=true when the whole item is gone.
- "change", "update", "set", "correct" => action "update".
- Questions like "what do I have", "what's expiring", "do I have milk" => action "query".
  * "what do I have" / "list" => query_type "list_all".
  * "what's expiring" / "about to go bad" => query_type "expiring_soon".
  * "do I have X" / "any X left" => query_type "have_item" with query_target = X.
- Resolve relative dates ("in 3 days", "next Friday") against TODAY given below.
- If you truly cannot tell, action "unknown" with empty items.
"""


class LLMParser:
    """Parse messages with an OpenAI-compatible chat completion client."""

    def __init__(self, client: Any, model: str = "gpt-4o-mini") -> None:
        self._client = client
        self._model = model

    def parse(self, message: str) -> ParsedAction:
        today = datetime.now(timezone.utc).date().isoformat()
        user_prompt = f"TODAY is {today}.\nMessage: {message}"
        response = self._client.chat.completions.create(
            model=self._model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        content = response.choices[0].message.content or "{}"
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return ParsedAction(action=ACTION_UNKNOWN, notes="LLM returned non-JSON")
        if not isinstance(data, dict):
            return ParsedAction(action=ACTION_UNKNOWN, notes="LLM returned non-object")
        return parsed_action_from_dict(data)


# ---------------------------------------------------------------------------
# Rule-based fallback parser (dependency-free, deterministic)
# ---------------------------------------------------------------------------
_ADD_VERBS = ("bought", "buy", "add", "got", "get", "picked up", "pick up", "restock", "put", "have added")
_REMOVE_VERBS = (
    "used up",
    "used the last of",
    "used",
    "ate",
    "eat",
    "finished",
    "finish",
    "ran out of",
    "run out of",
    "out of",
    "remove",
    "removed",
    "threw out",
    "throw out",
    "toss",
    "no more",
)
_REMOVE_ALL_HINTS = ("last of", "ran out", "run out", "finished", "no more", "all the", "used up")

_KNOWN_UNITS = {
    "carton", "cartons", "count", "g", "gram", "grams", "kg", "ml", "l", "litre",
    "liter", "liters", "pack", "packs", "packet", "bottle", "bottles", "can",
    "cans", "box", "boxes", "loaf", "loaves", "bunch", "dozen", "jar", "jars",
    "slice", "slices", "piece", "pieces", "bag", "bags",
}

_STOPWORDS = {"a", "an", "the", "some", "of", "my", "more", "few", "couple"}


class RuleBasedParser:
    """A small, deterministic heuristic parser (no network, no API key)."""

    def parse(self, message: str) -> ParsedAction:
        text = message.strip()
        lower = text.lower()
        if not text:
            return ParsedAction(action=ACTION_UNKNOWN)

        query = self._try_query(text, lower)
        if query is not None:
            return query

        # Remove before add so "used the last of ..." isn't caught by "add".
        for verb in _REMOVE_VERBS:
            if verb in lower:
                remainder = lower.split(verb, 1)[1]
                remove_all = any(h in lower for h in _REMOVE_ALL_HINTS)
                items = self._extract_items(remainder, force_remove_all=remove_all)
                if items:
                    return ParsedAction(
                        action=ACTION_REMOVE,
                        items=items,
                        notes=f"rule-based: matched remove verb '{verb}'",
                    )

        for verb in _ADD_VERBS:
            if re.search(rf"\b{re.escape(verb)}\b", lower):
                remainder = re.split(rf"\b{re.escape(verb)}\b", lower, maxsplit=1)[1]
                items = self._extract_items(remainder)
                if items:
                    return ParsedAction(
                        action=ACTION_ADD,
                        items=items,
                        notes=f"rule-based: matched add verb '{verb}'",
                    )

        # Bare "milk and eggs" with no verb -> assume add.
        items = self._extract_items(lower)
        if items:
            return ParsedAction(
                action=ACTION_ADD,
                items=items,
                notes="rule-based: no verb, defaulted to add",
            )

        return ParsedAction(action=ACTION_UNKNOWN, notes="rule-based: no match")

    # -- query detection ---------------------------------------------------
    def _try_query(self, text: str, lower: str) -> Optional[ParsedAction]:
        is_question = text.endswith("?")
        if any(k in lower for k in ("expiring", "expire", "going bad", "go bad", "about to go")):
            return ParsedAction(action=ACTION_QUERY, query_type=QUERY_EXPIRING)

        if any(k in lower for k in ("what do i have", "what's in", "whats in", "list", "inventory", "show me", "what have i got")):
            return ParsedAction(action=ACTION_QUERY, query_type=QUERY_LIST_ALL)

        have_match = re.search(r"(?:do i have|any|is there|have i got|got any)\s+(?:some\s+|any\s+)?([a-z][a-z\s]*?)(?:\s+left)?\??$", lower)
        if have_match and (is_question or "left" in lower):
            target = have_match.group(1).strip()
            target = " ".join(w for w in target.split() if w not in _STOPWORDS)
            if target:
                return ParsedAction(
                    action=ACTION_QUERY,
                    query_type=QUERY_HAVE_ITEM,
                    query_target=target,
                )
        return None

    # -- item extraction ---------------------------------------------------
    def _extract_items(self, chunk: str, force_remove_all: bool = False) -> list[ItemSpec]:
        chunk = chunk.strip(" .!?")
        if not chunk:
            return []
        parts = re.split(r"\s*(?:,|;|\band\b|&|\bplus\b)\s*", chunk)
        items: list[ItemSpec] = []
        for part in parts:
            spec = self._parse_one(part, force_remove_all)
            if spec is not None:
                items.append(spec)
        return items

    def _parse_one(self, part: str, force_remove_all: bool) -> Optional[ItemSpec]:
        tokens = [t for t in re.split(r"\s+", part.strip()) if t]
        tokens = [t for t in tokens if t not in _STOPWORDS or t == "of"]
        # Drop a leading "of" left over from phrases like "last of the cheese".
        while tokens and tokens[0] in _STOPWORDS:
            tokens.pop(0)
        if not tokens:
            return None

        qty: Optional[float] = None
        unit: Optional[str] = None

        if re.fullmatch(r"\d+(?:\.\d+)?", tokens[0]):
            qty = float(tokens[0])
            tokens.pop(0)
            if tokens and tokens[0] in _KNOWN_UNITS:
                unit = tokens.pop(0)
        elif tokens[0] == "dozen":
            qty = 12.0
            tokens.pop(0)

        # Trim trailing stopwords/units accidentally captured.
        name = " ".join(t for t in tokens if t not in _STOPWORDS).strip()
        if not name:
            return None
        return ItemSpec(
            item_name=name,
            item_qty=qty,
            unit=unit,
            category=infer_category(name),
            remove_all=force_remove_all,
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def build_parser(
    openai_api_key: str = "",
    model: str = "gpt-4o-mini",
) -> Parser:
    """Return an :class:`LLMParser` when an API key is present, else rule-based."""
    if openai_api_key:
        try:
            from openai import OpenAI  # imported lazily so it's optional
        except ImportError as exc:  # pragma: no cover - defensive
            raise RuntimeError(
                "OPENAI_API_KEY is set but the 'openai' package is not installed. "
                "Run: pip install -r requirements.txt"
            ) from exc
        client = OpenAI(api_key=openai_api_key)
        return LLMParser(client=client, model=model)
    return RuleBasedParser()
