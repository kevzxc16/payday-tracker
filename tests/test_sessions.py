"""Tests for app.sessions — create, load, destroy, expire."""
from __future__ import annotations

import time
from datetime import datetime, timedelta

from tests._helpers import TempDBTestCase


class SessionTests(TempDBTestCase):
    def _make_user(self) -> int:
        from app import db
        from app.security import hash_password
        h, s = hash_password("pw")
        cur = db.execute(
            """INSERT INTO users (email, password_hash, password_salt,
                                  pay_schedule, first_payday)
               VALUES (?, ?, ?, 'weekly', '2026-06-01')""",
            ("u@example.com", h, s),
        )
        return cur.lastrowid

    def test_create_and_load(self):
        from app import sessions
        uid = self._make_user()
        s = sessions.create_session(uid)
        self.assertIn("token", s)
        self.assertIn("csrf_token", s)
        loaded = sessions.load_session(s["token"])
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["email"], "u@example.com")
        self.assertEqual(loaded["csrf_token"], s["csrf_token"])

    def test_load_missing_token(self):
        from app import sessions
        self.assertIsNone(sessions.load_session(None))
        self.assertIsNone(sessions.load_session(""))
        self.assertIsNone(sessions.load_session("never-existed"))

    def test_destroy(self):
        from app import sessions
        uid = self._make_user()
        s = sessions.create_session(uid)
        sessions.destroy_session(s["token"])
        self.assertIsNone(sessions.load_session(s["token"]))

    def test_expired_session_returns_none_and_is_deleted(self):
        # Insert a session row with an expires_at in the past directly.
        from app import db, sessions
        uid = self._make_user()
        past = (datetime.utcnow() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        db.execute(
            "INSERT INTO sessions (token, user_id, csrf_token, expires_at) "
            "VALUES (?, ?, ?, ?)",
            ("expired-token", uid, "csrf", past),
        )
        # Loading it should return None…
        self.assertIsNone(sessions.load_session("expired-token"))
        # …and the row should be gone.
        row = db.query_one("SELECT * FROM sessions WHERE token = ?", ("expired-token",))
        self.assertIsNone(row)

    def test_purge_expired(self):
        from app import db, sessions
        uid = self._make_user()
        past = (datetime.utcnow() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        future = (datetime.utcnow() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        for tok, exp in [("a", past), ("b", past), ("c", future)]:
            db.execute(
                "INSERT INTO sessions (token, user_id, csrf_token, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (tok, uid, "csrf", exp),
            )
        n = sessions.purge_expired()
        self.assertEqual(n, 2)
        remaining = db.query("SELECT token FROM sessions ORDER BY token")
        self.assertEqual([r["token"] for r in remaining], ["c"])
