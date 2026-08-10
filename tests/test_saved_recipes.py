"""Tests for saved Instagram recipe helpers (offline)."""

from __future__ import annotations

import unittest

from fridge.models import SavedRecipe
from fridge.saved_recipes import (
    extract_instagram_url,
    extract_recipe_url,
    format_save_confirmation,
    format_saved_list,
    match_saved_recipes,
    normalize_instagram_url,
    normalize_keywords,
    parse_recipe_add_args,
    parse_recipe_add_message,
    score_saved_recipe,
    title_from_instagram_url,
    title_from_recipe_url,
)


def _saved(
    id: int,
    *,
    keywords: list[str],
    title: str = "Reel",
    url: str = "https://www.instagram.com/reel/abc/",
) -> SavedRecipe:
    return SavedRecipe(
        id=id,
        scope_key="user:test",
        added_by="test",
        url=url,
        title=title,
        keywords=keywords,
        created_ts="2026-01-01T00:00:00+00:00",
    )


class InstagramUrlTests(unittest.TestCase):
    def test_extract_and_strip_tracking(self):
        text = (
            "try this https://www.instagram.com/reel/AbC123XYZ/"
            "?utm_source=ig_web&igsh=foo korean spicy"
        )
        url = extract_instagram_url(text)
        self.assertEqual(url, "https://www.instagram.com/reel/AbC123XYZ")

    def test_normalize_trailing_slash(self):
        self.assertEqual(
            normalize_instagram_url("https://www.instagram.com/reel/xyz/"),
            "https://www.instagram.com/reel/xyz",
        )

    def test_no_url(self):
        self.assertIsNone(extract_instagram_url("just korean spicy"))

    def test_website_url(self):
        url = extract_recipe_url(
            "https://www.allrecipes.com/recipe/123/omelette/ korean"
        )
        self.assertEqual(url, "https://www.allrecipes.com/recipe/123/omelette")
        self.assertEqual(
            title_from_recipe_url(url), "Recipe from allrecipes.com"
        )

    def test_title_from_url(self):
        title = title_from_instagram_url("https://www.instagram.com/reel/AbC123XYZ")
        self.assertIn("reel", title.lower())
        self.assertIn("AbC123XYZ"[:8], title)


class KeywordParseTests(unittest.TestCase):
    def test_parse_command_args(self):
        url, kws = parse_recipe_add_args(
            [
                "https://www.instagram.com/reel/abc123/",
                "korean",
                "spicy",
            ]
        )
        self.assertEqual(url, "https://www.instagram.com/reel/abc123")
        self.assertEqual(kws, ["korean", "spicy"])

    def test_parse_message_with_url_mid_text(self):
        url, kws = parse_recipe_add_message(
            "https://www.instagram.com/reel/abc123/ mealprep airfryer"
        )
        self.assertIsNotNone(url)
        self.assertEqual(kws, ["mealprep", "airfryer"])

    def test_normalize_keywords_dedupes(self):
        self.assertEqual(
            normalize_keywords(["Korean", "korean", "SPICY"]),
            ["korean", "spicy"],
        )


class MatchTests(unittest.TestCase):
    def test_query_prefers_keyword_hits(self):
        recipes = [
            _saved(1, keywords=["italian", "pasta"], title="Pasta"),
            _saved(2, keywords=["korean", "spicy"], title="Kimchi fry"),
            _saved(3, keywords=["dessert"], title="Cake"),
        ]
        matched = match_saved_recipes(recipes, query="korean spicy", limit=2)
        self.assertEqual([r.id for r in matched], [2])

    def test_ingredient_overlap(self):
        recipes = [
            _saved(1, keywords=["eggs", "spinach"], title="Omelette"),
            _saved(2, keywords=["beef"], title="Steak"),
        ]
        matched = match_saved_recipes(
            recipes, query="", ingredients=["spinach", "milk"], limit=3
        )
        self.assertEqual([r.id for r in matched], [1])

    def test_no_query_falls_back_to_newest(self):
        recipes = [
            _saved(1, keywords=["a"]),
            _saved(5, keywords=["b"]),
            _saved(3, keywords=["c"]),
        ]
        matched = match_saved_recipes(recipes, query="", ingredients=[], limit=2)
        self.assertEqual([r.id for r in matched], [5, 3])

    def test_score_zero_when_no_overlap_with_query(self):
        r = _saved(1, keywords=["italian"])
        self.assertEqual(score_saved_recipe(r, query="korean"), 0)


class FormatTests(unittest.TestCase):
    def test_list_empty(self):
        text = format_saved_list([])
        self.assertIn("/recipe_add", text)

    def test_confirmation(self):
        r = _saved(9, keywords=["korean"], title="Kimchi")
        text = format_save_confirmation(r, created=True)
        self.assertIn("#9", text)
        self.assertIn("korean", text)
        self.assertIn("Saved", text)


if __name__ == "__main__":
    unittest.main()
