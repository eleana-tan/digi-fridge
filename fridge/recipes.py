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
You suggest simple home-cooking recipes based on ingredients the user has
and/or a style request (cuisine, dietary preference, meal type, etc.).
Respond with ONLY a JSON object:

{
  "recipes": [
    {
      "title": string,
      "summary": string,              // 1 short sentence
      "uses": [string, ...],          // ingredients from the provided list
      "missing": [string, ...],       // common pantry extras they may need
      "calories_per_portion": number, // REQUIRED rough kcal for 1 serving
      "protein_g": number,            // REQUIRED rough grams protein / serving
      "carbs_g": number,              // REQUIRED rough grams carbs / serving
      "fat_g": number,                // REQUIRED rough grams fat / serving
      "search_query": string,         // good web search for this recipe
      "url": string | null            // direct recipe URL only if you are
                                      // highly confident it is a real page
                                      // on a major site; otherwise null
    }
  ]
}

Rules:
- Suggest 3 recipes max, preferring ones that use many of the given ingredients.
- If the user gave a style request (e.g. "korean", "vegan", "quick dinner"),
  match that style while still using on-hand ingredients when listed.
- Prefer everyday dishes; mention if something is nearly out / stretchable.
- Nutrition is REQUIRED on every recipe: always set calories_per_portion,
  protein_g, carbs_g, and fat_g to realistic whole-number estimates for one
  adult home serving. Macros should roughly match calories (protein/carbs ≈
  4 kcal/g, fat ≈ 9). Prefer approximate numbers over omitting them — never
  leave all four null.
- search_query should be specific, e.g. "easy chicken stir fry recipe".
- Do NOT invent URLs. If unsure, set url to null (a search link will be added).
- If there are no ingredients AND no style request, return {"recipes": []}.
"""

# Words that mean "style/cuisine request", not grocery items.
_RECIPE_STYLE_TOKENS = frozenset(
    {
        "recipe",
        "recipes",
        "idea",
        "ideas",
        "meal",
        "meals",
        "dish",
        "dishes",
        "cuisine",
        "style",
        "korean",
        "italian",
        "chinese",
        "japanese",
        "thai",
        "indian",
        "mexican",
        "french",
        "mediterranean",
        "vietnamese",
        "malay",
        "singaporean",
        "american",
        "british",
        "spanish",
        "greek",
        "turkish",
        "middle-eastern",
        "vegan",
        "vegetarian",
        "pescatarian",
        "keto",
        "healthy",
        "light",
        "quick",
        "easy",
        "spicy",
        "comfort",
        "breakfast",
        "lunch",
        "dinner",
        "dessert",
        "snack",
        "soup",
        "salad",
        "bbq",
        "grill",
        "baked",
        "air-fryer",
        "airfryer",
        "one-pot",
        "weeknight",
    }
)


@dataclass
class RecipeIdea:
    title: str
    summary: str
    uses: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    calories_per_portion: Optional[int] = None  # rough kcal / serving
    protein_g: Optional[int] = None
    carbs_g: Optional[int] = None
    fat_g: Optional[int] = None
    url: str = ""  # always a clickable http(s) link after normalisation


class RecipeSuggester(Protocol):
    def suggest(
        self,
        ingredients: list[str],
        *,
        max_recipes: int = 3,
        request: str = "",
    ) -> list[RecipeIdea]:
        ...  # pragma: no cover


def split_recipe_command_args(args: list[str]) -> tuple[list[str], str]:
    """Interpret ``/recipe`` args as ingredients and/or a style request.

    Returns ``(ingredient_names, request_text)``.

    - ``/recipe eggs milk`` → ``(["eggs", "milk"], "")``
    - ``/recipe korean recipe`` → ``([], "korean recipe")`` (use fridge + style)
    - ``/recipe`` → ``([], "")`` (use fridge only)
    """
    tokens = [a.strip().lower() for a in args if a and a.strip()]
    if not tokens:
        return [], ""
    request_text = " ".join(tokens)
    if any(t in _RECIPE_STYLE_TOKENS for t in tokens):
        return [], request_text
    return tokens, ""


def search_url_for(query: str) -> str:
    """Build a reliable Google search URL for a recipe query."""
    q = quote_plus((query or "easy recipe").strip())
    return f"https://www.google.com/search?q={q}"


def _looks_like_http_url(value: str) -> bool:
    return bool(re.match(r"^https?://[^\s]+$", value.strip(), re.IGNORECASE))


def _coerce_bounded_int(
    value: Any, *, lo: int, hi: int
) -> Optional[int]:
    """Parse an int estimate; drop values outside ``lo``..``hi``."""
    if value is None or value == "":
        return None
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    if n < lo or n > hi:
        return None
    return n


def _coerce_calories(value: Any) -> Optional[int]:
    """Parse a calorie estimate; drop nonsense values."""
    return _coerce_bounded_int(value, lo=50, hi=2500)


def _coerce_macro_g(value: Any) -> Optional[int]:
    """Parse a macro gram estimate for one portion."""
    return _coerce_bounded_int(value, lo=0, hi=300)


def _first_present(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None and mapping[key] != "":
            return mapping[key]
    return None


def _nutrition_fields(entry: dict[str, Any]) -> tuple[
    Optional[int], Optional[int], Optional[int], Optional[int]
]:
    """Pull kcal/macros from canonical or alternate LLM key shapes."""
    nested = entry.get("nutrition") if isinstance(entry.get("nutrition"), dict) else {}
    merged = {**nested, **entry}  # top-level keys win
    calories = _coerce_calories(
        _first_present(
            merged, "calories_per_portion", "calories", "kcal", "calories_kcal"
        )
    )
    protein = _coerce_macro_g(_first_present(merged, "protein_g", "protein"))
    carbs = _coerce_macro_g(
        _first_present(
            merged, "carbs_g", "carbs", "carbohydrates_g", "carbohydrates"
        )
    )
    fat = _coerce_macro_g(_first_present(merged, "fat_g", "fat"))
    return calories, protein, carbs, fat


def _format_nutrition_line(recipe: RecipeIdea) -> Optional[str]:
    """One-line kcal + macros, or None if nothing useful is present."""
    bits: list[str] = []
    if recipe.calories_per_portion is not None:
        bits.append(f"~{recipe.calories_per_portion} kcal")
    macros: list[str] = []
    if recipe.protein_g is not None:
        macros.append(f"P {recipe.protein_g}g")
    if recipe.carbs_g is not None:
        macros.append(f"C {recipe.carbs_g}g")
    if recipe.fat_g is not None:
        macros.append(f"F {recipe.fat_g}g")
    if macros:
        bits.append(" · ".join(macros))
    if not bits:
        return None
    return " / ".join(bits) + " per portion (estimate)"


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
        calories, protein_g, carbs_g, fat_g = _nutrition_fields(entry)
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
                protein_g=protein_g,
                carbs_g=carbs_g,
                fat_g=fat_g,
                url=url,
            )
        )
    return out


def format_recipe_reply(
    recipes: list[RecipeIdea],
    ingredients: list[str],
    *,
    request: str = "",
) -> str:
    """Plain-language Telegram reply with links."""
    request = (request or "").strip()
    if not ingredients and not request:
        return (
            "I don't have any ingredients to work with. "
            "Add food to the fridge, or try: /recipe eggs milk spinach"
        )
    if not recipes:
        if request and not ingredients:
            return (
                f'I couldn\'t come up with ideas for "{request}". '
                "Try a different style, or add items to the fridge first."
            )
        return (
            "I couldn't come up with recipes for those ingredients. "
            "Try a different mix, or add more items to the fridge."
        )
    header_parts: list[str] = []
    if ingredients:
        based = "Based on: " + ", ".join(ingredients[:20])
        if len(ingredients) > 20:
            based += ", …"
        header_parts.append(based)
    if request:
        header_parts.append(f"Style: {request}")
    blocks = ["\n".join(header_parts), ""]
    for i, r in enumerate(recipes, 1):
        lines = [f"{i}. {r.title}"]
        if r.summary:
            lines.append(r.summary)
        nutrition = _format_nutrition_line(r)
        if nutrition:
            lines.append(nutrition)
        if r.uses:
            lines.append("Uses: " + ", ".join(r.uses))
        if r.missing:
            lines.append("You might also need: " + ", ".join(r.missing))
        lines.append(f"Recipe: {r.url}")
        blocks.append("\n".join(lines))
        blocks.append("")
    # Disclaimer once at the end — estimates, not lab values.
    blocks.append(
        "Calorie/macro figures are rough estimates, not exact nutrition facts."
    )
    return "\n".join(blocks).strip()


class LLMRecipeSuggester:
    """Suggest recipes via an OpenAI-compatible chat client."""

    def __init__(self, client: Any, model: str = "gpt-4o-mini") -> None:
        self._client = client
        self._model = model

    def suggest(
        self,
        ingredients: list[str],
        *,
        max_recipes: int = 3,
        request: str = "",
    ) -> list[RecipeIdea]:
        cleaned = sorted({i.strip().lower() for i in ingredients if i and i.strip()})
        request = (request or "").strip()
        if not cleaned and not request:
            return []
        parts: list[str] = []
        if cleaned:
            parts.append("Ingredients on hand:\n- " + "\n- ".join(cleaned))
        if request:
            parts.append(f"User request / style: {request}")
        parts.append(f"Suggest up to {max_recipes} recipes.")
        parts.append(
            "For EVERY recipe include integers for calories_per_portion, "
            "protein_g, carbs_g, and fat_g. Do not omit nutrition fields."
        )
        user_prompt = "\n\n".join(parts)
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
