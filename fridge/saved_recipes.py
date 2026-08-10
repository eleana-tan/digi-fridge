"""Helpers for user-saved recipe links (Instagram Reels, etc.).

Decoupled from Telegram: URL parsing, keyword normalisation, and matching
against a fridge / style query.
"""

from __future__ import annotations

import re
from typing import Iterable
from urllib.parse import urlparse, urlunparse

from .models import SavedRecipe

# Any http(s) link — Instagram reels, recipe sites, etc.
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

# Kept for tests / callers that specifically want Instagram.
_INSTAGRAM_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:instagram\.com|instagr\.am)/[^\s]+",
    re.IGNORECASE,
)

PENDING_RECIPE_ADD_PROMPT = (
    "Send me the link to your favorite recipe reel or website for future! "
    "Add keywords for better search"
)


def extract_recipe_url(text: str) -> str | None:
    """Return the first http(s) URL in ``text``, normalised, or None."""
    if not text:
        return None
    match = _URL_RE.search(text)
    if not match:
        return None
    return normalize_recipe_url(match.group(0).rstrip(").,];>'\"\\"))


def extract_instagram_url(text: str) -> str | None:
    """Return the first Instagram URL in ``text``, or None."""
    if not text:
        return None
    match = _INSTAGRAM_URL_RE.search(text)
    if not match:
        return None
    return normalize_recipe_url(match.group(0).rstrip(").,];>'\"\\"))


def normalize_recipe_url(url: str) -> str:
    """Strip tracking params and trailing slash noise; keep a stable canonical URL."""
    raw = (url or "").strip()
    parsed = urlparse(raw)
    # Drop query/fragment so the same link isn't saved twice with different trackers.
    clean = parsed._replace(query="", fragment="")
    path = clean.path.rstrip("/")
    return urlunparse(clean._replace(path=path or "/"))


# Backwards-compatible alias.
normalize_instagram_url = normalize_recipe_url


def title_from_recipe_url(url: str) -> str:
    """Best-effort title when we can't fetch the page caption."""
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower().removeprefix("www.")
    path = parsed.path.strip("/").split("/")
    if host in {"instagram.com", "instagr.am"}:
        if len(path) >= 2 and path[0] in {"reel", "reels", "p", "tv"}:
            kind = path[0].rstrip("s")
            return f"Instagram {kind} ({path[1][:12]})"
        return "Instagram recipe"
    if host:
        return f"Recipe from {host}"
    return "Saved recipe"


def title_from_instagram_url(url: str) -> str:
    """Alias for :func:`title_from_recipe_url`."""
    return title_from_recipe_url(url)


def normalize_keywords(words: Iterable[str]) -> list[str]:
    """Lowercase, strip, drop empties / pure URL tokens, dedupe (order kept)."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in words:
        token = (raw or "").strip().lower().strip(",;")
        if not token or token.startswith("http://") or token.startswith("https://"):
            continue
        if token in {"/", "-", "—", "|"}:
            continue
        # Allow multi-word keywords passed as one arg only when quoted elsewhere;
        # slash commands split on spaces so each arg is one keyword.
        if token not in seen:
            seen.add(token)
            out.append(token)
    return out


def parse_recipe_add_args(args: list[str]) -> tuple[str | None, list[str]]:
    """Parse ``/recipe_add`` args into ``(url, keywords)``."""
    text = " ".join(args or []).strip()
    return parse_recipe_add_message(text)


def parse_recipe_add_message(text: str) -> tuple[str | None, list[str]]:
    """Parse a freeform message that should contain a recipe URL + keywords."""
    text = text or ""
    url = extract_recipe_url(text)
    if not url:
        return None, []
    # Strip every URL occurrence (original form may include trackers or a
    # trailing slash that the normalised URL no longer has).
    remainder = _URL_RE.sub(" ", text)
    keywords = normalize_keywords(remainder.split())
    return url, keywords


def _tokens(*parts: str) -> set[str]:
    out: set[str] = set()
    for part in parts:
        for token in re.split(r"[\s,;/|]+", (part or "").lower()):
            token = token.strip()
            if len(token) >= 2:
                out.add(token)
    return out


def score_saved_recipe(
    recipe: SavedRecipe,
    *,
    query: str = "",
    ingredients: list[str] | None = None,
) -> int:
    """Higher score = better match. 0 means no overlap when a query was given."""
    haystack = _tokens(recipe.title, *recipe.keywords)
    query_tokens = _tokens(query)
    ingredient_tokens = _tokens(*(ingredients or []))

    score = 0
    for token in query_tokens:
        if token in haystack:
            score += 3
        elif any(token in h or h in token for h in haystack):
            score += 1
    for token in ingredient_tokens:
        if token in haystack:
            score += 2
        elif any(token in h or h in token for h in haystack):
            score += 1
    return score


def match_saved_recipes(
    recipes: list[SavedRecipe],
    *,
    query: str = "",
    ingredients: list[str] | None = None,
    limit: int = 3,
) -> list[SavedRecipe]:
    """Rank saved recipes by keyword / ingredient overlap.

    - With a query (e.g. ``/recipe korean``): only return positive scores.
    - With no query: return ingredient matches if any; otherwise the newest few
      (caller can still show them as "from your saved Reels").
    """
    if not recipes:
        return []
    query = (query or "").strip()
    ingredients = ingredients or []
    scored = [
        (score_saved_recipe(r, query=query, ingredients=ingredients), r)
        for r in recipes
    ]
    if query:
        positive = [(s, r) for s, r in scored if s > 0]
        positive.sort(key=lambda pair: (-pair[0], -pair[1].id))
        return [r for _, r in positive[:limit]]

    positive = [(s, r) for s, r in scored if s > 0]
    if positive:
        positive.sort(key=lambda pair: (-pair[0], -pair[1].id))
        return [r for _, r in positive[:limit]]

    # No keyword overlap — still surface a few recent saves as inspiration.
    newest = sorted(recipes, key=lambda r: r.id, reverse=True)
    return newest[:limit]


def format_keywords(keywords: list[str]) -> str:
    return ", ".join(keywords) if keywords else "(none)"


def format_save_confirmation(recipe: SavedRecipe, *, created: bool) -> str:
    verb = "Saved" if created else "Updated"
    return (
        f"{verb} recipe #{recipe.id}: {recipe.title}\n"
        f"Keywords: {format_keywords(recipe.keywords)}\n"
        f"{recipe.url}\n\n"
        "Tip: /recipe korean (or any keyword) will prefer matching saves. "
        "List with /reels, remove with /recipe_remove <id>."
    )


def format_saved_list(recipes: list[SavedRecipe]) -> str:
    if not recipes:
        return (
            "No saved recipes yet.\n"
            "Add one with:\n"
            "/recipe_add <url> keyword1 keyword2\n"
            "Or /recipe_add then paste the link (and keywords) in the next message."
        )
    lines = [f"Saved recipes ({len(recipes)}):", ""]
    for r in recipes:
        lines.append(f"#{r.id} {r.title}")
        lines.append(f"  Keywords: {format_keywords(r.keywords)}")
        lines.append(f"  {r.url}")
        lines.append("")
    lines.append("Remove one: /recipe_remove <id>")
    return "\n".join(lines).strip()


def format_saved_matches_section(recipes: list[SavedRecipe]) -> str:
    if not recipes:
        return ""
    blocks = ["From your saved Reels:", ""]
    for i, r in enumerate(recipes, 1):
        lines = [f"{i}. {r.title}"]
        if r.keywords:
            lines.append("Keywords: " + format_keywords(r.keywords))
        lines.append(r.url)
        blocks.append("\n".join(lines))
        blocks.append("")
    return "\n".join(blocks).strip()
