"""Tests for pending photo-draft edits (no Telegram)."""

from __future__ import annotations

import unittest

from fridge.models import ItemSpec
from fridge.pending import (
    STATUS_CANCELLED,
    STATUS_CONFIRMED,
    STATUS_ERROR,
    STATUS_NOT_AN_EDIT,
    STATUS_UPDATED,
    apply_pending_edit,
    format_pending,
)


def _items():
    return [
        ItemSpec(item_name="milk", item_qty=2, unit="carton"),
        ItemSpec(item_name="eggs", item_qty=12),
        ItemSpec(item_name="bread", item_qty=1, unit="loaf"),
    ]


class FormatPendingTests(unittest.TestCase):
    def test_format(self):
        text = format_pending(_items())
        self.assertIn("1. 2 carton milk", text)
        self.assertIn("2. 12 eggs", text)
        self.assertIn("3. 1 loaf bread", text)


class ApplyPendingEditTests(unittest.TestCase):
    def test_confirm(self):
        status, items, _ = apply_pending_edit(_items(), "yes")
        self.assertEqual(status, STATUS_CONFIRMED)
        self.assertEqual(len(items), 3)

    def test_cancel(self):
        status, items, _ = apply_pending_edit(_items(), "cancel")
        self.assertEqual(status, STATUS_CANCELLED)
        self.assertEqual(items, [])

    def test_remove_by_index(self):
        status, items, msg = apply_pending_edit(_items(), "remove 2")
        self.assertEqual(status, STATUS_UPDATED)
        self.assertEqual([i.item_name for i in items], ["milk", "bread"])
        self.assertIn("eggs", msg)

    def test_remove_by_name(self):
        status, items, _ = apply_pending_edit(_items(), "remove milk")
        self.assertEqual(status, STATUS_UPDATED)
        self.assertEqual([i.item_name for i in items], ["eggs", "bread"])

    def test_remove_bad_index(self):
        status, items, msg = apply_pending_edit(_items(), "remove 9")
        self.assertEqual(status, STATUS_ERROR)
        self.assertEqual(len(items), 3)
        self.assertIn("#9", msg)

    def test_change_line(self):
        status, items, msg = apply_pending_edit(_items(), "change 1 to 3 cartons milk")
        self.assertEqual(status, STATUS_UPDATED)
        self.assertEqual(items[0].item_name, "milk")
        self.assertEqual(items[0].item_qty, 3.0)
        self.assertIn("Updated #1", msg)

    def test_unrelated_message(self):
        status, items, _ = apply_pending_edit(_items(), "what's expiring soon?")
        self.assertEqual(status, STATUS_NOT_AN_EDIT)
        self.assertEqual(len(items), 3)


if __name__ == "__main__":
    unittest.main()
