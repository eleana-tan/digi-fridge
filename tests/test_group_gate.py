"""Tests for group-chat response gating (no Telegram network)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from fridge.group_gate import (
    is_addressed_to_bot,
    looks_like_fridge_intent,
    should_handle_in_group,
    strip_bot_mention,
)


class FridgeIntentTests(unittest.TestCase):
    def test_fridge_phrases(self):
        for text in (
            "bought milk",
            "used the last of the cheese",
            "what do we have?",
            "recipes with eggs",
            "what's expiring soon?",
            "who bought the milk?",
        ):
            self.assertTrue(looks_like_fridge_intent(text), text)

    def test_chitchat_ignored(self):
        for text in (
            "hey everyone",
            "lol",
            "see you tomorrow",
            "how was your day?",
            "thanks!",
        ):
            self.assertFalse(looks_like_fridge_intent(text), text)


class StripMentionTests(unittest.TestCase):
    def test_strips_mention(self):
        self.assertEqual(
            strip_bot_mention("@FridgeBot bought milk", "FridgeBot"),
            "bought milk",
        )


class AddressedTests(unittest.TestCase):
    def test_reply_to_bot(self):
        msg = SimpleNamespace(
            reply_to_message=SimpleNamespace(from_user=SimpleNamespace(id=99)),
            text="yes",
            caption=None,
            entities=None,
            caption_entities=None,
        )
        self.assertTrue(is_addressed_to_bot(msg, bot_id=99, bot_username="FridgeBot"))

    def test_literal_mention(self):
        msg = SimpleNamespace(
            reply_to_message=None,
            text="@FridgeBot list please",
            caption=None,
            entities=None,
            caption_entities=None,
        )
        self.assertTrue(is_addressed_to_bot(msg, bot_id=1, bot_username="FridgeBot"))


class ShouldHandleTests(unittest.TestCase):
    def test_private_always(self):
        self.assertTrue(
            should_handle_in_group(
                is_group=False,
                message=None,
                bot_id=1,
                bot_username="x",
                text="hello",
            )
        )

    def test_group_chitchat_silent(self):
        msg = SimpleNamespace(
            reply_to_message=None,
            text="hey folks",
            caption=None,
            entities=None,
            caption_entities=None,
        )
        self.assertFalse(
            should_handle_in_group(
                is_group=True,
                message=msg,
                bot_id=1,
                bot_username="FridgeBot",
                text="hey folks",
            )
        )

    def test_group_fridge_intent(self):
        msg = SimpleNamespace(
            reply_to_message=None,
            text="bought eggs",
            caption=None,
            entities=None,
            caption_entities=None,
        )
        self.assertTrue(
            should_handle_in_group(
                is_group=True,
                message=msg,
                bot_id=1,
                bot_username="FridgeBot",
                text="bought eggs",
            )
        )


if __name__ == "__main__":
    unittest.main()
