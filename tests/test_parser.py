"""Parser tests - all run offline, no Telegram, no network.

They cover three things:
1. The pure JSON -> ParsedAction transform (parsed_action_from_dict).
2. LLMParser wired to a *stub* client returning canned JSON.
3. The dependency-free RuleBasedParser on realistic sample messages.
"""

from __future__ import annotations

import json
import unittest

from fridge.models import (
    ACTION_ADD,
    ACTION_QUERY,
    ACTION_REMOVE,
    ACTION_UNKNOWN,
    QUERY_EXPIRING,
    QUERY_HAVE_ITEM,
    QUERY_LIST_ALL,
    ParsedAction,
)
from fridge.parser import (
    HybridParser,
    LLMParser,
    RuleBasedParser,
    infer_category,
    parsed_action_from_dict,
)


class ParsedActionFromDictTests(unittest.TestCase):
    def test_add_with_full_fields(self):
        data = {
            "action": "add",
            "items": [
                {
                    "item_name": "Milk",
                    "item_qty": 2,
                    "unit": "carton",
                    "expires_on": "2026-08-12",
                    "category": "dairy",
                }
            ],
        }
        action = parsed_action_from_dict(data)
        self.assertEqual(action.action, ACTION_ADD)
        self.assertEqual(len(action.items), 1)
        item = action.items[0]
        self.assertEqual(item.item_name, "Milk")
        self.assertEqual(item.item_qty, 2.0)
        self.assertEqual(item.unit, "carton")
        self.assertEqual(item.expires_on, "2026-08-12")
        self.assertEqual(item.category, "dairy")

    def test_remove_all_flag(self):
        data = {
            "action": "remove",
            "items": [{"item_name": "cheese", "remove_all": True}],
        }
        action = parsed_action_from_dict(data)
        self.assertEqual(action.action, ACTION_REMOVE)
        self.assertTrue(action.items[0].remove_all)

    def test_query_have_item(self):
        data = {"action": "query", "query_type": "have_item", "query_target": "milk"}
        action = parsed_action_from_dict(data)
        self.assertEqual(action.action, ACTION_QUERY)
        self.assertEqual(action.query_type, QUERY_HAVE_ITEM)
        self.assertEqual(action.query_target, "milk")

    def test_invalid_action_collapses_to_unknown(self):
        action = parsed_action_from_dict({"action": "frobnicate"})
        self.assertEqual(action.action, ACTION_UNKNOWN)

    def test_invalid_query_type_dropped(self):
        action = parsed_action_from_dict(
            {"action": "query", "query_type": "nonsense"}
        )
        self.assertIsNone(action.query_type)

    def test_category_inferred_when_missing(self):
        action = parsed_action_from_dict(
            {"action": "add", "items": [{"item_name": "cheddar cheese"}]}
        )
        self.assertEqual(action.items[0].category, "dairy")

    def test_items_without_name_are_skipped(self):
        action = parsed_action_from_dict(
            {"action": "add", "items": [{"item_qty": 3}, {"item_name": "eggs"}]}
        )
        self.assertEqual(len(action.items), 1)
        self.assertEqual(action.items[0].item_name, "eggs")

    def test_various_date_formats_normalized(self):
        for raw, expected in [
            ("2026-08-12", "2026-08-12"),
            ("2026/08/12", "2026-08-12"),
            ("12-08-2026", "2026-08-12"),
            ("08/12/2026", "2026-08-12"),
            ("2026-08-12T00:00:00", "2026-08-12"),
            ("someday", None),
        ]:
            action = parsed_action_from_dict(
                {"action": "add", "items": [{"item_name": "x", "expires_on": raw}]}
            )
            self.assertEqual(action.items[0].expires_on, expected, raw)


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
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return _StubResponse(self._content)


class _StubChat:
    def __init__(self, content):
        self.completions = _StubCompletions(content)


class _StubClient:
    """Mimics the tiny slice of the OpenAI client that LLMParser uses."""

    def __init__(self, content):
        self.chat = _StubChat(content)


class LLMParserTests(unittest.TestCase):
    def test_parses_canned_json(self):
        payload = json.dumps(
            {
                "action": "add",
                "items": [{"item_name": "milk", "item_qty": 1, "unit": "carton"}],
            }
        )
        client = _StubClient(payload)
        parser = LLMParser(client=client, model="test-model")
        action = parser.parse("bought a carton of milk")
        self.assertEqual(action.action, ACTION_ADD)
        self.assertEqual(action.items[0].item_name, "milk")
        # It sent our model + JSON response_format through.
        kwargs = client.chat.completions.last_kwargs
        self.assertEqual(kwargs["model"], "test-model")
        self.assertEqual(kwargs["response_format"], {"type": "json_object"})

    def test_non_json_response_is_unknown(self):
        parser = LLMParser(client=_StubClient("not json at all"))
        action = parser.parse("hi")
        self.assertEqual(action.action, ACTION_UNKNOWN)

    def test_temperature_omitted_by_default(self):
        # Newer models reject a non-default temperature, so we must not send it
        # unless explicitly configured.
        client = _StubClient('{"action": "unknown"}')
        LLMParser(client=client).parse("hi")
        self.assertNotIn("temperature", client.chat.completions.last_kwargs)

    def test_temperature_sent_when_configured(self):
        client = _StubClient('{"action": "unknown"}')
        LLMParser(client=client, temperature=0).parse("hi")
        self.assertEqual(client.chat.completions.last_kwargs["temperature"], 0)

    def test_latency_options_forwarded(self):
        client = _StubClient('{"action": "unknown"}')
        LLMParser(
            client=client, reasoning_effort="minimal", max_completion_tokens=200
        ).parse("hi")
        kwargs = client.chat.completions.last_kwargs
        self.assertEqual(kwargs["reasoning_effort"], "minimal")
        self.assertEqual(kwargs["max_completion_tokens"], 200)


class _RecordingParser:
    """Test double that records whether it was called and returns a canned action."""

    def __init__(self, action):
        self.action = action
        self.called = False

    def parse(self, message):
        self.called = True
        return self.action


class HybridParserTests(unittest.TestCase):
    def test_simple_message_uses_fast_path(self):
        slow = _RecordingParser(ParsedAction(action=ACTION_UNKNOWN))
        hybrid = HybridParser(fast=RuleBasedParser(), slow=slow)
        result = hybrid.parse("bought milk and eggs")
        self.assertEqual(result.action, ACTION_ADD)
        self.assertFalse(slow.called)  # LLM not consulted

    def test_date_message_defers_to_llm(self):
        canned = ParsedAction(action=ACTION_ADD)
        slow = _RecordingParser(canned)
        hybrid = HybridParser(fast=RuleBasedParser(), slow=slow)
        hybrid.parse("bought milk that expires tomorrow")
        self.assertTrue(slow.called)  # deferred to LLM due to date hint

    def test_low_confidence_defers_to_llm(self):
        canned = ParsedAction(action=ACTION_ADD)
        slow = _RecordingParser(canned)
        hybrid = HybridParser(fast=RuleBasedParser(), slow=slow)
        # Gibberish the rule parser can't confidently handle -> LLM.
        hybrid.parse("hmm not sure")
        self.assertTrue(slow.called)


class RuleBasedParserTests(unittest.TestCase):
    def setUp(self):
        self.parser = RuleBasedParser()

    def test_add_multiple_items(self):
        action = self.parser.parse("bought milk and eggs")
        self.assertEqual(action.action, ACTION_ADD)
        names = sorted(i.item_name for i in action.items)
        self.assertEqual(names, ["eggs", "milk"])

    def test_add_with_quantity_and_unit(self):
        action = self.parser.parse("bought 2 cartons of milk")
        self.assertEqual(action.action, ACTION_ADD)
        item = action.items[0]
        self.assertEqual(item.item_name, "milk")
        self.assertEqual(item.item_qty, 2.0)
        self.assertEqual(item.unit, "cartons")

    def test_dozen_expands_quantity(self):
        action = self.parser.parse("got a dozen eggs")
        item = action.items[0]
        self.assertEqual(item.item_qty, 12.0)
        self.assertEqual(item.item_name, "eggs")

    def test_remove_last_of(self):
        action = self.parser.parse("used the last of the cheese")
        self.assertEqual(action.action, ACTION_REMOVE)
        self.assertEqual(action.items[0].item_name, "cheese")
        self.assertTrue(action.items[0].remove_all)

    def test_query_expiring(self):
        action = self.parser.parse("what's expiring soon?")
        self.assertEqual(action.action, ACTION_QUERY)
        self.assertEqual(action.query_type, QUERY_EXPIRING)

    def test_query_list_all(self):
        action = self.parser.parse("what do I have?")
        self.assertEqual(action.action, ACTION_QUERY)
        self.assertEqual(action.query_type, QUERY_LIST_ALL)

    def test_query_have_item(self):
        action = self.parser.parse("do I have milk?")
        self.assertEqual(action.action, ACTION_QUERY)
        self.assertEqual(action.query_type, QUERY_HAVE_ITEM)
        self.assertEqual(action.query_target, "milk")

    def test_bare_items_default_to_add(self):
        action = self.parser.parse("milk, bread, eggs")
        self.assertEqual(action.action, ACTION_ADD)
        self.assertEqual(len(action.items), 3)

    def test_infer_category(self):
        self.assertEqual(infer_category("whole milk"), "dairy")
        self.assertEqual(infer_category("chicken breast"), "meat")
        self.assertIsNone(infer_category("mystery goo"))


if __name__ == "__main__":
    unittest.main()
