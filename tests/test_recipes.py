"""Tests for recipe inspiration helpers (offline, no network)."""

from __future__ import annotations

import unittest

from fridge.recipes import (
    LLMRecipeSuggester,
    format_recipe_reply,
    recipes_from_dict,
    search_url_for,
)


class RecipesFromDictTests(unittest.TestCase):
    def test_builds_search_url_when_no_direct_link(self):
        ideas = recipes_from_dict(
            {
                "recipes": [
                    {
                        "title": "Veggie Stir Fry",
                        "summary": "Quick weeknight dinner.",
                        "uses": ["eggs", "spinach"],
                        "missing": ["soy sauce"],
                        "calories_per_portion": 420,
                        "protein_g": 18,
                        "carbs_g": 35,
                        "fat_g": 22,
                        "search_query": "easy veggie stir fry",
                        "url": None,
                    }
                ]
            }
        )
        self.assertEqual(len(ideas), 1)
        self.assertEqual(ideas[0].title, "Veggie Stir Fry")
        self.assertEqual(ideas[0].calories_per_portion, 420)
        self.assertEqual(ideas[0].protein_g, 18)
        self.assertEqual(ideas[0].carbs_g, 35)
        self.assertEqual(ideas[0].fat_g, 22)
        self.assertIn("google.com/search", ideas[0].url)
        self.assertIn("stir", ideas[0].url.lower())

    def test_calories_out_of_range_dropped(self):
        ideas = recipes_from_dict(
            {
                "recipes": [
                    {
                        "title": "Snack",
                        "summary": "Tiny.",
                        "uses": ["apple"],
                        "calories_per_portion": 10,
                    }
                ]
            }
        )
        self.assertIsNone(ideas[0].calories_per_portion)

    def test_keeps_plausible_direct_url(self):
        ideas = recipes_from_dict(
            {
                "recipes": [
                    {
                        "title": "Omelette",
                        "summary": "Classic.",
                        "uses": ["eggs"],
                        "missing": [],
                        "search_query": "classic omelette",
                        "url": "https://www.allrecipes.com/recipe/123/omelette/",
                    }
                ]
            }
        )
        self.assertEqual(
            ideas[0].url, "https://www.allrecipes.com/recipe/123/omelette/"
        )

    def test_rejects_fake_non_http_url(self):
        ideas = recipes_from_dict(
            {
                "recipes": [
                    {
                        "title": "Soup",
                        "summary": "Warm.",
                        "uses": ["onion"],
                        "url": "not-a-url",
                    }
                ]
            }
        )
        self.assertTrue(ideas[0].url.startswith("https://"))

    def test_empty_recipes(self):
        self.assertEqual(recipes_from_dict({"recipes": []}), [])


class FormatReplyTests(unittest.TestCase):
    def test_includes_link_and_ingredients(self):
        ideas = recipes_from_dict(
            {
                "recipes": [
                    {
                        "title": "Pasta",
                        "summary": "Simple.",
                        "uses": ["pasta", "cheese"],
                        "missing": ["garlic"],
                        "calories_per_portion": 550,
                        "protein_g": 20,
                        "carbs_g": 70,
                        "fat_g": 18,
                        "search_query": "simple cheese pasta",
                    }
                ]
            }
        )
        text = format_recipe_reply(ideas, ["pasta", "cheese"])
        self.assertIn("Based on: pasta, cheese", text)
        self.assertIn("Pasta", text)
        self.assertIn("~550 kcal", text)
        self.assertIn("P 20g", text)
        self.assertIn("C 70g", text)
        self.assertIn("F 18g", text)
        self.assertIn("Recipe: http", text)
        self.assertIn("garlic", text)
        self.assertIn("rough estimates", text)

    def test_empty_ingredients_message(self):
        text = format_recipe_reply([], [])
        self.assertIn("/recipe", text)


class SearchUrlTests(unittest.TestCase):
    def test_encodes_spaces(self):
        url = search_url_for("chicken stir fry recipe")
        self.assertIn("chicken", url)
        self.assertNotIn(" ", url)


class _StubMessage:
    def __init__(self, content):
        self.content = content


class _StubChoice:
    def __init__(self, content):
        self.message = _StubMessage(content)


class _StubResponse:
    def __init__(self, content):
        self.choices = [_StubChoice(content)]


class _StubCompletions:
    def __init__(self, content):
        self._content = content

    def create(self, **kwargs):
        return _StubResponse(self._content)


class _StubChat:
    def __init__(self, content):
        self.completions = _StubCompletions(content)


class _StubClient:
    def __init__(self, content):
        self.chat = _StubChat(content)


class LLMRecipeSuggesterTests(unittest.TestCase):
    def test_parses_canned_json(self):
        payload = """
        {"recipes":[{"title":"Fried Rice","summary":"Uses leftovers.",
          "uses":["rice","eggs"],"missing":["soy sauce"],
          "search_query":"easy egg fried rice","url":null}]}
        """
        suggester = LLMRecipeSuggester(client=_StubClient(payload))
        ideas = suggester.suggest(["rice", "eggs"])
        self.assertEqual(len(ideas), 1)
        self.assertEqual(ideas[0].title, "Fried Rice")
        self.assertTrue(ideas[0].url.startswith("https://"))

    def test_empty_ingredients_short_circuits(self):
        suggester = LLMRecipeSuggester(client=_StubClient("{}"))
        self.assertEqual(suggester.suggest([]), [])


if __name__ == "__main__":
    unittest.main()
