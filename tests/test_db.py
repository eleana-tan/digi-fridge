"""Database layer tests, run against an in-memory SQLite database."""

from __future__ import annotations

import sqlite3
import unittest

from fridge import db


class DBTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.migrate(self.conn)

    def tearDown(self):
        self.conn.close()


class MigrationTests(DBTestCase):
    def test_migrations_are_idempotent(self):
        # Running again should not raise or duplicate.
        db.migrate(self.conn)
        rows = self.conn.execute("SELECT version FROM schema_migrations").fetchall()
        self.assertEqual([r[0] for r in rows], [1, 2])

    def test_scope_column_exists(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(inventory)")}
        self.assertIn("scope_key", cols)

    def test_tables_exist(self):
        names = {
            r[0]
            for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertIn("inventory", names)
        self.assertIn("action_log", names)
        self.assertIn("users", names)


class InventoryWriteTests(DBTestCase):
    def test_add_creates_then_increments(self):
        item, created = db.add_or_increment(self.conn, "u1", "milk", 1, "carton")
        self.assertTrue(created)
        self.assertEqual(item.item_qty, 1)

        item2, created2 = db.add_or_increment(self.conn, "u1", "Milk", 2)
        self.assertFalse(created2)  # case-insensitive match
        self.assertEqual(item2.item_qty, 3)
        self.assertEqual(item2.unit, "carton")  # preserved

    def test_add_defaults_qty_to_one(self):
        item, _ = db.add_or_increment(self.conn, "u1", "eggs")
        self.assertEqual(item.item_qty, 1)

    def test_remove_reduce_and_delete(self):
        db.add_or_increment(self.conn, "u1", "milk", 3)
        status, item = db.remove_quantity(self.conn, "u1", "milk", 1)
        self.assertEqual(status, "reduced")
        self.assertEqual(item.item_qty, 2)

        status, _ = db.remove_quantity(self.conn, "u1", "milk", 5)
        self.assertEqual(status, "deleted")
        self.assertIsNone(db.find_item(self.conn, "u1", "milk"))

    def test_remove_all_deletes(self):
        db.add_or_increment(self.conn, "u1", "cheese", 5)
        status, _ = db.remove_quantity(self.conn, "u1", "cheese", remove_all=True)
        self.assertEqual(status, "deleted")

    def test_remove_missing_item(self):
        status, item = db.remove_quantity(self.conn, "u1", "ghost", 1)
        self.assertEqual(status, "not_found")
        self.assertIsNone(item)

    def test_update_item(self):
        db.add_or_increment(self.conn, "u1", "milk", 1)
        updated = db.update_item(
            self.conn, "u1", "milk", item_qty=4, expires_on="2026-08-20"
        )
        self.assertEqual(updated.item_qty, 4)
        self.assertEqual(updated.expires_on, "2026-08-20")

    def test_updated_ts_changes_on_increment(self):
        item, _ = db.add_or_increment(self.conn, "u1", "milk", 1)
        # Force a different timestamp by updating created row directly.
        self.conn.execute(
            "UPDATE inventory SET created_ts='2000-01-01T00:00:00+00:00',"
            " updated_ts='2000-01-01T00:00:00+00:00' WHERE id=?",
            (item.id,),
        )
        self.conn.commit()
        item2, _ = db.add_or_increment(self.conn, "u1", "milk", 1)
        self.assertNotEqual(item2.updated_ts, item2.created_ts)


class InventoryReadTests(DBTestCase):
    def test_get_items_isolated_per_user(self):
        db.add_or_increment(self.conn, "u1", "milk")
        db.add_or_increment(self.conn, "u2", "eggs")
        self.assertEqual(len(db.get_items(self.conn, "u1")), 1)
        self.assertEqual(len(db.get_items(self.conn, "u2")), 1)

    def test_search_items(self):
        db.add_or_increment(self.conn, "u1", "whole milk")
        db.add_or_increment(self.conn, "u1", "almond milk")
        db.add_or_increment(self.conn, "u1", "bread")
        matches = db.search_items(self.conn, "u1", "milk")
        self.assertEqual(len(matches), 2)

    def test_get_expiring(self):
        db.add_or_increment(self.conn, "u1", "milk", expires_on="2026-08-10")
        db.add_or_increment(self.conn, "u1", "eggs", expires_on="2026-08-20")
        db.add_or_increment(self.conn, "u1", "salt")  # no expiry
        soon = db.get_expiring(self.conn, "u1", within_days=2, today="2026-08-09")
        names = [i.item_name for i in soon]
        self.assertEqual(names, ["milk"])

    def test_scopes_with_expiring(self):
        db.add_or_increment(self.conn, "u1", "milk", expires_on="2026-08-10")
        db.add_or_increment(self.conn, "u2", "eggs", expires_on="2026-08-30")
        scopes = db.scopes_with_expiring(self.conn, within_days=2, today="2026-08-09")
        self.assertEqual(scopes, ["u1"])


class GroupScopeTests(DBTestCase):
    def test_two_users_keep_separate_attributed_rows(self):
        scope = "chat:123"
        db.add_or_increment(self.conn, scope, "milk", 2, "carton", added_by="alice")
        db.add_or_increment(self.conn, scope, "milk", 1, "carton", added_by="bob")
        items = db.get_items(self.conn, scope)
        self.assertEqual(len(items), 2)
        by_user = {i.user_id: i.item_qty for i in items}
        self.assertEqual(by_user, {"alice": 2, "bob": 1})

    def test_same_user_increments_not_duplicates(self):
        scope = "chat:123"
        db.add_or_increment(self.conn, scope, "milk", 1, added_by="alice")
        db.add_or_increment(self.conn, scope, "milk", 2, added_by="alice")
        items = db.get_items(self.conn, scope)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].item_qty, 3)

    def test_search_returns_all_contributors(self):
        scope = "chat:123"
        db.add_or_increment(self.conn, scope, "milk", 2, added_by="alice")
        db.add_or_increment(self.conn, scope, "milk", 1, added_by="bob")
        matches = db.search_items(self.conn, scope, "milk")
        self.assertEqual({m.user_id for m in matches}, {"alice", "bob"})

    def test_remove_prefers_callers_own_item(self):
        scope = "chat:123"
        db.add_or_increment(self.conn, scope, "milk", 2, added_by="alice")
        db.add_or_increment(self.conn, scope, "milk", 1, added_by="bob")
        status, item = db.remove_quantity(
            self.conn, scope, "milk", remove_all=True, prefer_user="bob"
        )
        self.assertEqual(status, "deleted")
        self.assertEqual(item.user_id, "bob")
        remaining = db.get_items(self.conn, scope)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].user_id, "alice")

    def test_clear_scope(self):
        scope = "chat:123"
        other = "chat:999"
        db.add_or_increment(self.conn, scope, "milk", 1, added_by="alice")
        db.add_or_increment(self.conn, scope, "eggs", 6, added_by="bob")
        db.add_or_increment(self.conn, other, "bread", 1, added_by="carol")
        removed = db.clear_scope(self.conn, scope)
        self.assertEqual(removed, 2)
        self.assertEqual(db.get_items(self.conn, scope), [])
        self.assertEqual(len(db.get_items(self.conn, other)), 1)


class UserMappingTests(DBTestCase):
    def test_remember_and_get_chat_id(self):
        db.remember_user(self.conn, "alice", 12345)
        self.assertEqual(db.get_chat_id(self.conn, "alice"), 12345)
        # Upsert updates the chat id.
        db.remember_user(self.conn, "alice", 67890)
        self.assertEqual(db.get_chat_id(self.conn, "alice"), 67890)

    def test_get_chat_id_missing(self):
        self.assertIsNone(db.get_chat_id(self.conn, "nobody"))


class ActionLogTests(DBTestCase):
    def test_log_action(self):
        db.log_action(self.conn, "u1", "bought milk", '{"action": "add"}')
        rows = self.conn.execute("SELECT * FROM action_log").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["raw_message"], "bought milk")


if __name__ == "__main__":
    unittest.main()
