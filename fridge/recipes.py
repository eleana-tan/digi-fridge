"""Recipe inspiration from a list of ingredients.

Decoupled from Telegram. Takes ingredient names, asks an LLM for ideas, and
attaches **working** website links. LLMs often invent dead recipe URLs, so every
suggestion gets a real search URL on a major recipe index (plus any plausible
direct URL the model returns).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol
from urllib.parse import quote_plus

_SYSTEM_PROMPT = """\
You suggest simple home-cooking recipes based on ingredients the user has.
Respond with ONLY a JSON object:

{
  "recipes": [
    {
      "title": string,
      "summary": string,              // 1 short sentence
      "uses": [string, ...],          // ingredients from the provided list
      "missing": [string, ...],       // common pantry extras they may need
      "calories_per_portion": number | null,  // rough kcal estimate for 1 serving
      "search_query": string,         // good web search for this recipe
      "url": string | null            // direct recipe URL only if you are
                                      // highly confident it is a real page
                                      // on a major site; otherwise null
    }
  ]
}

Rules:
- Suggest 3 recipes max, preferring ones that use many of the given ingredients.
- Prefer everyday dishes; mention if something is nearly out / stretchable.
- calories_per_portion: best-effort estimate in kcal for one adult serving
  (typical home portion). Round to a sensible whole number (e.g. 350, 520).
  If you truly cannot estimate, use null — do not invent extremes.
- search_query should be specific, e.g. "easy chicken stir fry recipe".
- Do NOT invent URLs. If unsure, set url to null (a search link will be added).
- If the ingredient list is empty, return {"recipes": []}.
"""


@dataclass
class RecipeIdea:
    title: str
    summary: str
    uses: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    calories_per_portion: Optional[int] = None  # rough kcal / serving
    url: str = ""  # always a clickable http(s) link after normalisation


class RecipeSuggester(Protocol):
    def suggest(self, ingredients: list[str], *, max_recipes: int = 3) -> list[RecipeIdea]:
        ...  # pragma: no cover


def search_url_for(query: str) -> str:
    """Build a reliable Google search URL for a recipe query."""
    q = quote_plus((query or "easy recipe").strip())
    return f"https://www.google.com/search?q={q}"


def _looks_like_http_url(value: str) -> bool:
    return bool(re.match(r"^https?://[^\s]+$", value.strip(), re.IGNORECASE))


def _coerce_calories(value: Any) -> Optional[int]:
    """Parse a calorie estimate; drop nonsense values."""
    if value is None or value == "":
        return None
    try:
        kcal = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    # Sanity bounds for a single adult portion.
    if kcal < 50 or kcal > 2500:
        return None
    return kcal


def recipes_from_dict(data: dict[str, Any], *, max_recipes: int = 3) -> list[RecipeIdea]:
    """Pure transform: LLM JSON -> list of RecipeIdea (unit-testable)."""
    raw = data.get("recipes") or []
    if not isinstance(raw, list):
        return []
    out: list[RecipeIdea] = []
    for entry in raw[:max_recipes]:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or "").strip()
        if not title:
            continue
        summary = str(entry.get("summary") or "").strip()
        uses = [str(x).strip() for x in (entry.get("uses") or []) if str(x).strip()]
        missing = [
            str(x).strip() for x in (entry.get("missing") or []) if str(x).strip()
        ]
        calories = _coerce_calories(entry.get("calories_per_portion"))
        search_query = str(entry.get("search_query") or title).strip() or title
        direct = str(entry.get("url") or "").strip()
        if direct and _looks_like_http_url(direct):
            url = direct
        else:
            # Prefer a recipe-focused search so the link always works.
            if "recipe" not in search_query.lower():
                search_query = f"{search_query} recipe"
            url = search_url_for(search_query)
        out.append(
            RecipeIdea(
                title=title,
                summary=summary,
                uses=uses,
                missing=missing,
                calories_per_portion=calories,
                url=url,
            )
        )
    return out


def format_recipe_reply(
    recipes: list[RecipeIdea], ingredients: list[str]
) -> str:
    """Plain-language Telegram reply with links."""
    if not ingredients:
        return (
            "I don't have any ingredients to work with. "
            "Add food to the fridge, or try: /recipe eggs milk spinach"
        )
    if not recipes:
        return (
            "I couldn't come up with recipes for those ingredients. "
            "Try a different mix, or add more items to the fridge."
        )
    header = "Based on: " + ", ".join(ingredients[:20])
    if len(ingredients) > 20:
        header += ", …"
    blocks = [header, ""]
    for i, r in enumerate(recipes, 1):
        lines = [f"{i}. {r.title}"]
        if r.summary:
            lines.append(r.summary)
        if r.calories_per_portion is not None:
            lines.append(
                f"~{r.calories_per_portion} kcal per portion (estimate)"
            )
        if r.uses:
            lines.append("Uses: " + ", ".join(r.uses))
        if r.missing:
            lines.append("You might also need: " + ", ".join(r.missing))
        lines.append(f"Recipe: {r.url}")
        blocks.append("\n".join(lines))
        blocks.append("")
    # Disclaimer once at the end — estimates, not lab values.
    blocks.append("Calorie figures are rough estimates, not exact nutrition facts.")
    return "\n".join(blocks).strip()


class LLMRecipeSuggester:
    """Suggest recipes via an OpenAI-compatible chat client."""

    def __init__(self, client: Any, model: str = "gpt-4o-mini") -> None:
        self._client = client
        self._model = model

    def suggest(
        self, ingredients: list[str], *, max_recipes: int = 3
    ) -> list[RecipeIdea]:
        cleaned = sorted({i.strip().lower() for i in ingredients if i and i.strip()})
        if not cleaned:
            return []
        user_prompt = (
            f"Ingredients on hand:\n- "
            + "\n- ".join(cleaned)
            + f"\n\nSuggest up to {max_recipes} recipes."
        )
        response = self._client.chat.completions.create(
            model=self._model,
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
            return []
        if not isinstance(data, dict):
            return []
        return recipes_from_dict(data, max_recipes=max_recipes)


def build_recipe_suggester(
    openai_api_key: str = "",
    model: str = "gpt-4o-mini",
) -> Optional[LLMRecipeSuggester]:
    """Return a suggester, or None if no API key is set."""
    if not openai_api_key:
        return None
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "OPENAI_API_KEY is set but the 'openai' package is not installed. "
            "Run: pip install -r requirements.txt"
        ) from exc
    return LLMRecipeSuggester(client=OpenAI(api_key=openai_api_key), model=model)
