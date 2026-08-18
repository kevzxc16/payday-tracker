"""Tests for app.notifications — generation rules and dispatch."""
from __future__ import annotations

from datetime import date, timedelta

from tests._helpers import TempDBTestCase


class GenerationTests(TempDBTestCase):
    def setUp(self) -> None:
        super().setUp()
        from app import db
        from app.security import hash_password
        h, s = hash_password("pw")
        self.today = date.today()
        cur = db.execute(
            """INSERT INTO users (email, password_hash, password_salt,
                                   pay_schedule, first_payday)
               VALUES (?, ?, ?, 'biweekly', ?)""",
            ("u@example.com", h, s, self.today.isoformat()),
        )
        self.user_id = cur.lastrowid
        self.user = db.query_one("SELECT * FROM users WHERE id = ?", (self.user_id,))

    def _pending(self, type: str | None = None) -> list[dict]:
        from app import db
        sql = "SELECT * FROM notifications WHERE user_id = ?"
        params: list = [self.user_id]
        if type:
            sql += " AND type = ?"
            params.append(type)
        sql += " ORDER BY id"
        return db.query(sql, params)

    # ----- welcome -----

    def test_welcome_enqueues(self):
        from app import notifications as N
        N.send_welcome(self.user_id, self.user["email"])
        rows = self._pending("welcome")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "pending")

    # ----- bill_due -----

    def test_bill_due_within_lead_window(self):
        from app import db, notifications as N
        due = (self.today + timedelta(days=2)).isoformat()
        db.execute("""INSERT INTO bills (user_id, name, amount_cents, due_date,
                                         is_recurring, status)
                      VALUES (?, 'Rent', 100000, ?, 0, 'unpaid')""",
                   (self.user_id, due))
        gen = N.generate_for_user(self.user, on=self.today)
        self.assertEqual(gen["bill_due"], 1)

    def test_bill_due_beyond_window_skipped(self):
        from app import db, notifications as N
        due = (self.today + timedelta(days=30)).isoformat()
        db.execute("""INSERT INTO bills (user_id, name, amount_cents, due_date,
                                         is_recurring, status)
                      VALUES (?, 'Rent', 100000, ?, 0, 'unpaid')""",
                   (self.user_id, due))
        gen = N.generate_for_user(self.user, on=self.today)
        self.assertEqual(gen["bill_due"], 0)

    def test_paid_bill_not_reminded(self):
        from app import db, notifications as N
        due = (self.today + timedelta(days=1)).isoformat()
        db.execute("""INSERT INTO bills (user_id, name, amount_cents, due_date,
                                         is_recurring, status, paid_on)
                      VALUES (?, 'Rent', 100000, ?, 0, 'paid', ?)""",
                   (self.user_id, due, self.today.isoformat()))
        gen = N.generate_for_user(self.user, on=self.today)
        self.assertEqual(gen["bill_due"], 0)

    def test_bill_due_idempotent(self):
        from app import db, notifications as N
        due = (self.today + timedelta(days=1)).isoformat()
        db.execute("""INSERT INTO bills (user_id, name, amount_cents, due_date,
                                         is_recurring, status)
                      VALUES (?, 'Rent', 100000, ?, 0, 'unpaid')""",
                   (self.user_id, due))
        N.generate_for_user(self.user, on=self.today)
        N.generate_for_user(self.user, on=self.today)
        N.generate_for_user(self.user, on=self.today)
        # Only one bill_due reminder despite 3 runs
        self.assertEqual(len(self._pending("bill_due")), 1)

    # ----- savings_reminder -----

    def test_savings_reminder_on_payday(self):
        from app import db, notifications as N
        db.execute("INSERT INTO savings_goals (user_id, name, "
                   "target_amount_cents, status) VALUES (?, 'EF', 500000, 'active')",
                   (self.user_id,))
        # today IS first_payday so this is a payday
        gen = N.generate_for_user(self.user, on=self.today)
        self.assertEqual(gen["savings_reminder"], 1)

    def test_no_savings_reminder_without_active_goal(self):
        from app import notifications as N
        gen = N.generate_for_user(self.user, on=self.today)
        self.assertEqual(gen["savings_reminder"], 0)

    def test_no_savings_reminder_off_payday(self):
        from app import db, notifications as N
        db.execute("INSERT INTO savings_goals (user_id, name, "
                   "target_amount_cents, status) VALUES (?, 'EF', 500000, 'active')",
                   (self.user_id,))
        # 3 days after payday (still in period, but not on payday itself)
        gen = N.generate_for_user(self.user, on=self.today + timedelta(days=3))
        self.assertEqual(gen["savings_reminder"], 0)

    # ----- debt_due -----

    def test_debt_due_when_payoff_date_near(self):
        from app import db, notifications as N
        target = (self.today + timedelta(days=4)).isoformat()
        db.execute("""INSERT INTO debts (user_id, name, starting_balance_cents,
                                         current_balance_cents,
                                         minimum_payment_cents,
                                         target_payoff_date, status)
                      VALUES (?, 'CC', 300000, 250000, 5000, ?, 'active')""",
                   (self.user_id, target))
        gen = N.generate_for_user(self.user, on=self.today)
        self.assertEqual(gen["debt_due"], 1)

    def test_no_debt_reminder_without_target_date(self):
        from app import db, notifications as N
        db.execute("""INSERT INTO debts (user_id, name, starting_balance_cents,
                                         current_balance_cents,
                                         minimum_payment_cents, status)
                      VALUES (?, 'CC', 300000, 250000, 5000, 'active')""",
                   (self.user_id,))
        gen = N.generate_for_user(self.user, on=self.today)
        self.assertEqual(gen["debt_due"], 0)

    # ----- period_checkin -----

    def test_period_checkin_at_end_of_period(self):
        from app import notifications as N
        end_of_period = self.today + timedelta(days=13)
        gen = N.generate_for_user(self.user, on=end_of_period)
        self.assertEqual(gen["period_checkin"], 1)

    def test_no_period_checkin_mid_period(self):
        from app import notifications as N
        gen = N.generate_for_user(self.user, on=self.today + timedelta(days=5))
        self.assertEqual(gen["period_checkin"], 0)

    # ----- dispatch -----

    def test_dispatch_pending_console_mode(self):
        from app import notifications as N
        N.send_welcome(self.user_id, self.user["email"])
        summary = N.dispatch_pending()
        self.assertEqual(summary["sent"], 1)
        self.assertEqual(summary["failed"], 0)
        rows = self._pending("welcome")
        self.assertEqual(rows[0]["status"], "sent")
        self.assertIsNotNone(rows[0]["sent_at"])

    def test_cancel_pending(self):
        from app import notifications as N
        N.send_welcome(self.user_id, self.user["email"])
        notif_id = self._pending("welcome")[0]["id"]
        ok = N.cancel(self.user_id, notif_id)
        self.assertTrue(ok)
        # Re-cancelling does nothing
        self.assertFalse(N.cancel(self.user_id, notif_id))
        self.assertEqual(self._pending("welcome")[0]["status"], "cancelled")

    def test_cancelled_notification_not_dispatched(self):
        from app import notifications as N
        N.send_welcome(self.user_id, self.user["email"])
        notif_id = self._pending("welcome")[0]["id"]
        N.cancel(self.user_id, notif_id)
        summary = N.dispatch_pending()
        self.assertEqual(summary["sent"], 0)
