"""Vision extractor tests - offline, using a stub chat client (no network)."""

from __future__ import annotations

import json
import unittest

from fridge.vision import OpenAIImageExtractor, build_image_extractor


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
    def __init__(self, content):
        self.chat = _StubChat(content)


class ImageExtractorTests(unittest.TestCase):
    def test_extracts_items_from_canned_json(self):
        payload = json.dumps(
            {
                "items": [
                    {"item_name": "Milk", "item_qty": 2, "unit": "carton"},
                    {"item_name": "eggs", "item_qty": 12},
                ]
            }
        )
        client = _StubClient(payload)
        extractor = OpenAIImageExtractor(client=client, model="gpt-4o-mini")
        items = extractor.extract(b"\xff\xd8\xff", "image/jpeg")
        names = sorted(i.item_name for i in items)
        self.assertEqual(names, ["Milk", "eggs"])
        # It sent JSON mode and an image_url part in the message.
        kwargs = client.chat.completions.last_kwargs
        self.assertEqual(kwargs["response_format"], {"type": "json_object"})
        user_content = kwargs["messages"][1]["content"]
        self.assertTrue(any(p.get("type") == "image_url" for p in user_content))

    def test_empty_on_bad_json(self):
        extractor = OpenAIImageExtractor(client=_StubClient("not json"))
        self.assertEqual(extractor.extract(b"x"), [])

    def test_no_items_key(self):
        extractor = OpenAIImageExtractor(client=_StubClient('{"foo": 1}'))
        self.assertEqual(extractor.extract(b"x"), [])

    def test_build_returns_none_without_key(self):
        self.assertIsNone(build_image_extractor(openai_api_key=""))


if __name__ == "__main__":
    unittest.main()
