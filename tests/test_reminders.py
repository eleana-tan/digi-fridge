"""Tests for the pure reminder-building logic (no Telegram delivery)."""

from __future__ import annotations

import sqlite3
import unittest

from fridge import db, reminders


class ReminderTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.migrate(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_build_reminders_for_expiring_user(self):
        db.remember_user(self.conn, "alice", 111)
        db.add_or_increment(self.conn, "alice", "milk", 1, expires_on="2026-08-10")
        db.add_or_increment(self.conn, "alice", "salt")  # no expiry, ignored

        result = reminders.build_reminders(
            self.conn, within_days=2, today="2026-08-09"
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].chat_id, 111)
        self.assertIn("milk", result[0].text)
        self.assertNotIn("salt", result[0].text)

    def test_user_without_chat_id_is_skipped(self):
        # Item exists but user never messaged the bot -> no chat id known.
        db.add_or_increment(self.conn, "bob", "eggs", 1, expires_on="2026-08-10")
        result = reminders.build_reminders(
            self.conn, within_days=2, today="2026-08-09"
        )
        self.assertEqual(result, [])

    def test_no_expiring_items(self):
        db.remember_user(self.conn, "alice", 111)
        db.add_or_increment(self.conn, "alice", "milk", 1, expires_on="2026-12-31")
        result = reminders.build_reminders(
            self.conn, within_days=2, today="2026-08-09"
        )
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
