"""Tests for the parse -> DB -> reply layer (no Telegram)."""

from __future__ import annotations

import sqlite3
import unittest

from fridge import actions, db
from fridge.models import (
    ACTION_ADD,
    ACTION_QUERY,
    ACTION_REMOVE,
    ACTION_UNKNOWN,
    QUERY_EXPIRING,
    QUERY_HAVE_ITEM,
    QUERY_LIST_ALL,
    QUERY_BY_USER,
    QUERY_WHO_HAS,
    ItemSpec,
    ParsedAction,
)


class ActionsTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.migrate(self.conn)
        self.user = "u1"

    def tearDown(self):
        self.conn.close()

    def test_add_reply(self):
        action = ParsedAction(
            action=ACTION_ADD,
            items=[ItemSpec(item_name="milk", item_qty=2, unit="carton")],
        )
        reply = actions.execute(self.conn, self.user, action)
        self.assertIn("Added 2 carton milk", reply)
        self.assertEqual(db.find_item(self.conn, self.user, "milk").item_qty, 2)

    def test_remove_reply(self):
        db.add_or_increment(self.conn, self.user, "cheese", 1)
        action = ParsedAction(
            action=ACTION_REMOVE,
            items=[ItemSpec(item_name="cheese", remove_all=True)],
        )
        reply = actions.execute(self.conn, self.user, action)
        self.assertIn("Removed all cheese", reply)

    def test_remove_missing_reply(self):
        action = ParsedAction(
            action=ACTION_REMOVE, items=[ItemSpec(item_name="tofu")]
        )
        reply = actions.execute(self.conn, self.user, action)
        self.assertIn("no tofu", reply)

    def test_query_list_all(self):
        db.add_or_increment(self.conn, self.user, "milk", 1)
        db.add_or_increment(self.conn, self.user, "eggs", 6)
        action = ParsedAction(action=ACTION_QUERY, query_type=QUERY_LIST_ALL)
        reply = actions.execute(self.conn, self.user, action)
        self.assertIn("milk", reply)
        self.assertIn("eggs", reply)

    def test_query_have_item_yes(self):
        db.add_or_increment(self.conn, self.user, "whole milk", 1)
        action = ParsedAction(
            action=ACTION_QUERY, query_type=QUERY_HAVE_ITEM, query_target="milk"
        )
        reply = actions.execute(self.conn, self.user, action)
        self.assertIn("Yes", reply)

    def test_query_have_item_no(self):
        action = ParsedAction(
            action=ACTION_QUERY, query_type=QUERY_HAVE_ITEM, query_target="milk"
        )
        reply = actions.execute(self.conn, self.user, action)
        self.assertIn("No", reply)

    def test_query_expiring(self):
        db.add_or_increment(self.conn, self.user, "milk", 1, expires_on="1999-01-01")
        action = ParsedAction(action=ACTION_QUERY, query_type=QUERY_EXPIRING)
        reply = actions.execute(self.conn, self.user, action)
        self.assertIn("Expiring soon", reply)

    def test_unknown_reply(self):
        reply = actions.execute(
            self.conn, self.user, ParsedAction(action=ACTION_UNKNOWN)
        )
        self.assertIn("didn't quite catch that", reply)


class GroupAttributionTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.migrate(self.conn)
        self.scope = "chat:99"

    def tearDown(self):
        self.conn.close()

    def _add(self, action, added_by, is_group=True):
        return actions.execute(
            self.conn, self.scope, action, added_by=added_by, is_group=is_group
        )

    def test_add_attributed_per_user(self):
        self._add(
            ParsedAction(action=ACTION_ADD, items=[ItemSpec(item_name="milk", item_qty=2)]),
            added_by="alice",
        )
        self._add(
            ParsedAction(action=ACTION_ADD, items=[ItemSpec(item_name="milk", item_qty=1)]),
            added_by="bob",
        )
        items = db.get_items(self.conn, self.scope)
        self.assertEqual({i.user_id for i in items}, {"alice", "bob"})

    def test_who_has_query(self):
        self._add(
            ParsedAction(action=ACTION_ADD, items=[ItemSpec(item_name="milk", item_qty=2)]),
            added_by="alice",
        )
        reply = actions.execute(
            self.conn,
            self.scope,
            ParsedAction(action=ACTION_QUERY, query_type=QUERY_WHO_HAS, query_target="milk"),
            is_group=True,
        )
        self.assertIn("@alice", reply)
        self.assertIn("milk", reply)

    def test_list_all_hides_buyer_unless_asked(self):
        # Buyer names only on explicit attribution queries (user decision).
        self._add(
            ParsedAction(action=ACTION_ADD, items=[ItemSpec(item_name="eggs", item_qty=6)]),
            added_by="carol",
        )
        reply = actions.execute(
            self.conn,
            self.scope,
            ParsedAction(action=ACTION_QUERY, query_type=QUERY_LIST_ALL),
            is_group=True,
        )
        self.assertIn("eggs", reply)
        self.assertNotIn("@carol", reply)
        self.assertIn("group has", reply)

    def test_by_user_query(self):
        self._add(
            ParsedAction(action=ACTION_ADD, items=[ItemSpec(item_name="milk", item_qty=1)]),
            added_by="alice",
        )
        self._add(
            ParsedAction(action=ACTION_ADD, items=[ItemSpec(item_name="bread", item_qty=1)]),
            added_by="bob",
        )
        reply = actions.execute(
            self.conn,
            self.scope,
            ParsedAction(
                action=ACTION_QUERY, query_type=QUERY_BY_USER, query_target="alice"
            ),
            is_group=True,
        )
        self.assertIn("@alice", reply)
        self.assertIn("milk", reply)
        self.assertNotIn("bread", reply)


if __name__ == "__main__":
    unittest.main()
