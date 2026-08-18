"""Tests for app.services.budget.summarize_for_user."""
from __future__ import annotations

from datetime import date, timedelta

from tests._helpers import TempDBTestCase


class BudgetTests(TempDBTestCase):
    """Seed a user with a fixed period and verify the summary aggregates."""

    def setUp(self) -> None:
        super().setUp()
        from app import db
        from app.security import hash_password
        h, s = hash_password("pw")
        # Anchor first payday = today; biweekly period is today → today+13.
        self.today = date.today()
        self.period_start = self.today
        self.period_end = self.today + timedelta(days=13)
        cur = db.execute(
            """INSERT INTO users (email, password_hash, password_salt,
                                   pay_schedule, first_payday)
               VALUES (?, ?, ?, 'biweekly', ?)""",
            ("u@example.com", h, s, self.today.isoformat()),
        )
        self.user_id = cur.lastrowid
        self.user = db.query_one("SELECT * FROM users WHERE id = ?", (self.user_id,))

    def test_empty_summary(self):
        from app.services.budget import summarize_for_user
        s = summarize_for_user(self.user, on=self.today)
        self.assertEqual(s.income_cents, 0)
        self.assertEqual(s.bills_due_cents, 0)
        self.assertEqual(s.expenses_cents, 0)
        self.assertEqual(s.savings_cents, 0)
        self.assertEqual(s.debt_paid_cents, 0)
        self.assertEqual(s.discretionary_remaining_cents, 0)
        self.assertEqual(s.period.start, self.period_start)
        self.assertEqual(s.period.end, self.period_end)

    def test_full_summary(self):
        from app import db
        from app.services.budget import summarize_for_user

        mid_period = self.today + timedelta(days=5)
        # Income: 2 paychecks in period totalling $2000
        db.execute("INSERT INTO paychecks (user_id, amount_cents, received_on) "
                   "VALUES (?, ?, ?)", (self.user_id, 150000, self.today.isoformat()))
        db.execute("INSERT INTO paychecks (user_id, amount_cents, received_on) "
                   "VALUES (?, ?, ?)", (self.user_id, 50000, mid_period.isoformat()))

        # Bills: one $1200 paid, one $50 unpaid, both due this period
        db.execute("""INSERT INTO bills (user_id, name, amount_cents, due_date,
                                         is_recurring, status, paid_on)
                      VALUES (?, 'Rent', 120000, ?, 0, 'paid', ?)""",
                   (self.user_id, self.today.isoformat(), self.today.isoformat()))
        db.execute("""INSERT INTO bills (user_id, name, amount_cents, due_date,
                                         is_recurring, status)
                      VALUES (?, 'Internet', 5000, ?, 0, 'unpaid')""",
                   (self.user_id, mid_period.isoformat()))

        # Expenses: $75 total
        db.execute("INSERT INTO expenses (user_id, amount_cents, category, spent_on) "
                   "VALUES (?, 5000, 'Groceries', ?)",
                   (self.user_id, self.today.isoformat()))
        db.execute("INSERT INTO expenses (user_id, amount_cents, category, spent_on) "
                   "VALUES (?, 2500, 'Coffee', ?)",
                   (self.user_id, mid_period.isoformat()))

        # Savings: $200 toward a goal
        db.execute("INSERT INTO savings_goals (user_id, name, target_amount_cents) "
                   "VALUES (?, 'EF', 500000)", (self.user_id,))
        goal_id = db.query_one("SELECT id FROM savings_goals WHERE user_id = ?",
                                (self.user_id,))["id"]
        db.execute("INSERT INTO savings_contributions (goal_id, amount_cents, "
                   "contributed_on) VALUES (?, 20000, ?)",
                   (goal_id, self.today.isoformat()))

        # Debt: starting $3000, paid $500
        db.execute("""INSERT INTO debts (user_id, name, starting_balance_cents,
                                         current_balance_cents,
                                         minimum_payment_cents, status)
                      VALUES (?, 'CC', 300000, 250000, 5000, 'active')""",
                   (self.user_id,))
        debt_id = db.query_one("SELECT id FROM debts WHERE user_id = ?",
                                (self.user_id,))["id"]
        db.execute("INSERT INTO debt_payments (debt_id, amount_cents, paid_on) "
                   "VALUES (?, 50000, ?)", (debt_id, self.today.isoformat()))

        s = summarize_for_user(self.user, on=self.today)
        self.assertEqual(s.income_cents, 200000)
        self.assertEqual(s.bills_due_cents, 125000)
        self.assertEqual(s.bills_paid_cents, 120000)
        self.assertEqual(s.bills_unpaid_cents, 5000)
        self.assertEqual(s.expenses_cents, 7500)
        self.assertEqual(s.savings_cents, 20000)
        self.assertEqual(s.debt_paid_cents, 50000)
        # 200000 - 5000 - 120000 - 20000 - 50000 - 7500 = -2500
        self.assertEqual(s.discretionary_remaining_cents, -2500)
        self.assertEqual(len(s.active_goals), 1)
        self.assertEqual(len(s.active_debts), 1)

    def test_out_of_period_data_excluded(self):
        from app import db
        from app.services.budget import summarize_for_user

        last_period = self.today - timedelta(days=14)
        db.execute("INSERT INTO paychecks (user_id, amount_cents, received_on) "
                   "VALUES (?, 100000, ?)",
                   (self.user_id, last_period.isoformat()))
        s = summarize_for_user(self.user, on=self.today)
        # Income from the previous period must not be counted.
        self.assertEqual(s.income_cents, 0)

    def test_overdue_count(self):
        from app import db
        from app.services.budget import summarize_for_user

        yesterday = (self.today - timedelta(days=1)).isoformat()
        db.execute("""INSERT INTO bills (user_id, name, amount_cents, due_date,
                                         is_recurring, status)
                      VALUES (?, 'OldBill', 1000, ?, 0, 'unpaid')""",
                   (self.user_id, yesterday))
        s = summarize_for_user(self.user, on=self.today)
        self.assertEqual(s.bills_count_overdue, 1)
