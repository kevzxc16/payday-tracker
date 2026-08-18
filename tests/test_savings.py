"""
Tests for app.services.savings.paycheck_plan_for_goal.

The function is pure (no DB), so these tests just construct dict-like
'goal' and 'user' fixtures and assert on the returned plan.
"""
from __future__ import annotations

import unittest
from datetime import date, timedelta


def _user(schedule: str, first_payday: date) -> dict:
    return {
        "id": 1,
        "pay_schedule": schedule,
        "first_payday": first_payday.isoformat(),
    }


def _goal(
    *,
    target_cents: int = 100000,
    saved_cents: int = 0,
    deadline: date | None,
    status: str = "active",
) -> dict:
    return {
        "id": 42,
        "name": "Test goal",
        "target_amount_cents": target_cents,
        "saved_cents": saved_cents,
        "deadline": deadline.isoformat() if deadline else None,
        "status": status,
    }


class PaycheckPlanTests(unittest.TestCase):
    """Math + edge cases for paycheck_plan_for_goal."""

    def setUp(self) -> None:
        # All tests run with a known 'today' so weekday math is predictable.
        self.today = date(2026, 1, 5)  # Monday

    # ------------------------------------------------------------------
    # Returns-None cases
    # ------------------------------------------------------------------

    def test_no_deadline_returns_none(self):
        from app.services.savings import paycheck_plan_for_goal
        user = _user("biweekly", self.today)
        goal = _goal(deadline=None)
        self.assertIsNone(paycheck_plan_for_goal(goal, user, on=self.today))

    def test_non_active_goal_returns_none(self):
        from app.services.savings import paycheck_plan_for_goal
        user = _user("biweekly", self.today)
        for status in ("achieved", "paused", "cancelled"):
            goal = _goal(deadline=self.today + timedelta(days=90), status=status)
            self.assertIsNone(
                paycheck_plan_for_goal(goal, user, on=self.today),
                f"status={status!r} should produce no plan",
            )

    # ------------------------------------------------------------------
    # Status: on_track (normal case)
    # ------------------------------------------------------------------

    def test_biweekly_even_split(self):
        """$1000 over 10 biweekly paychecks → $100 each."""
        from app.services.savings import paycheck_plan_for_goal
        user = _user("biweekly", self.today)
        # Anchor today + 10 paychecks = today + 140 days. Set deadline
        # ON that payday so the 10th payday counts.
        deadline = self.today + timedelta(days=14 * 10)
        goal = _goal(target_cents=100000, deadline=deadline)

        plan = paycheck_plan_for_goal(goal, user, on=self.today)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.status, "on_track")
        self.assertEqual(plan.paychecks_remaining, 10)
        self.assertEqual(plan.per_paycheck_cents, 10000)
        self.assertEqual(plan.remaining_cents, 100000)

    def test_weekly_even_split(self):
        """$520 over 52 weekly paychecks → $10 each."""
        from app.services.savings import paycheck_plan_for_goal
        user = _user("weekly", self.today)
        deadline = self.today + timedelta(days=7 * 52)
        goal = _goal(target_cents=52000, deadline=deadline)

        plan = paycheck_plan_for_goal(goal, user, on=self.today)
        self.assertEqual(plan.status, "on_track")
        self.assertEqual(plan.paychecks_remaining, 52)
        self.assertEqual(plan.per_paycheck_cents, 1000)

    def test_uneven_split_rounds_up(self):
        """$1000 over 3 paychecks → ceil($333.33) = $333.34."""
        from app.services.savings import paycheck_plan_for_goal
        user = _user("biweekly", self.today)
        deadline = self.today + timedelta(days=14 * 3)
        goal = _goal(target_cents=100000, deadline=deadline)

        plan = paycheck_plan_for_goal(goal, user, on=self.today)
        self.assertEqual(plan.status, "on_track")
        self.assertEqual(plan.paychecks_remaining, 3)
        # ceil(100000 / 3) = 33334 cents = $333.34
        self.assertEqual(plan.per_paycheck_cents, 33334)
        # Three payments of $333.34 = $1000.02 (slightly over) — preferable
        # to flooring and missing the goal by $0.01.
        self.assertGreaterEqual(plan.per_paycheck_cents * 3, 100000)

    def test_subtracts_already_saved(self):
        """Saved $400 toward $1000 over 6 paydays → $100/paycheck."""
        from app.services.savings import paycheck_plan_for_goal
        user = _user("biweekly", self.today)
        deadline = self.today + timedelta(days=14 * 6)
        goal = _goal(target_cents=100000, saved_cents=40000, deadline=deadline)

        plan = paycheck_plan_for_goal(goal, user, on=self.today)
        self.assertEqual(plan.status, "on_track")
        self.assertEqual(plan.remaining_cents, 60000)
        self.assertEqual(plan.per_paycheck_cents, 10000)

    # ------------------------------------------------------------------
    # Status: fully_funded
    # ------------------------------------------------------------------

    def test_fully_funded(self):
        from app.services.savings import paycheck_plan_for_goal
        user = _user("biweekly", self.today)
        goal = _goal(
            target_cents=50000,
            saved_cents=50000,
            deadline=self.today + timedelta(days=60),
        )
        plan = paycheck_plan_for_goal(goal, user, on=self.today)
        self.assertEqual(plan.status, "fully_funded")
        self.assertEqual(plan.per_paycheck_cents, 0)
        self.assertEqual(plan.remaining_cents, 0)
        self.assertEqual(plan.paychecks_remaining, 0)

    def test_overfunded_treated_as_fully_funded(self):
        """Saved more than the target — still 'fully_funded', not negative."""
        from app.services.savings import paycheck_plan_for_goal
        user = _user("biweekly", self.today)
        goal = _goal(
            target_cents=50000,
            saved_cents=60000,
            deadline=self.today + timedelta(days=60),
        )
        plan = paycheck_plan_for_goal(goal, user, on=self.today)
        self.assertEqual(plan.status, "fully_funded")
        self.assertEqual(plan.remaining_cents, 0)

    # ------------------------------------------------------------------
    # Status: deadline_passed
    # ------------------------------------------------------------------

    def test_deadline_in_past(self):
        from app.services.savings import paycheck_plan_for_goal
        user = _user("biweekly", self.today)
        goal = _goal(
            target_cents=100000,
            saved_cents=30000,
            deadline=self.today - timedelta(days=5),
        )
        plan = paycheck_plan_for_goal(goal, user, on=self.today)
        self.assertEqual(plan.status, "deadline_passed")
        # The "per paycheck" amount in this case is the full remaining — the
        # template should NOT render this as a per-paycheck value, but
        # rather as the still-needed total.
        self.assertEqual(plan.per_paycheck_cents, 70000)
        self.assertEqual(plan.paychecks_remaining, 0)

    # ------------------------------------------------------------------
    # Status: due_this_period
    # ------------------------------------------------------------------

    def test_due_before_next_payday(self):
        """Deadline falls inside the current pay period (before next payday)."""
        from app.services.savings import paycheck_plan_for_goal
        user = _user("biweekly", self.today)  # anchor=today, next payday=today+14
        # Deadline 5 days from now — before the next payday.
        goal = _goal(
            target_cents=20000,
            saved_cents=5000,
            deadline=self.today + timedelta(days=5),
        )
        plan = paycheck_plan_for_goal(goal, user, on=self.today)
        self.assertEqual(plan.status, "due_this_period")
        self.assertEqual(plan.per_paycheck_cents, 15000)
        self.assertEqual(plan.paychecks_remaining, 0)

    # ------------------------------------------------------------------
    # Monthly schedule with month-end edge case
    # ------------------------------------------------------------------

    def test_monthly_schedule(self):
        """6 monthly paychecks → divides correctly."""
        from app.services.savings import paycheck_plan_for_goal
        # Anchor on Jan 5; the 6th monthly payday is Jul 5.
        anchor = date(2026, 1, 5)
        user = _user("monthly", anchor)
        goal = _goal(target_cents=60000, deadline=date(2026, 7, 5))

        plan = paycheck_plan_for_goal(goal, user, on=anchor)
        self.assertEqual(plan.status, "on_track")
        self.assertEqual(plan.paychecks_remaining, 6)
        self.assertEqual(plan.per_paycheck_cents, 10000)

    def test_monthly_anchor_jan31_clamps(self):
        """Jan-31 anchor should still produce a valid plan even though
        February clamps to Feb-28."""
        from app.services.savings import paycheck_plan_for_goal
        anchor = date(2026, 1, 31)
        user = _user("monthly", anchor)
        # Deadline is end of April. Paydays after Jan 31: Feb 28, Mar 31, Apr 30.
        goal = _goal(target_cents=30000, deadline=date(2026, 4, 30))

        plan = paycheck_plan_for_goal(goal, user, on=anchor)
        self.assertEqual(plan.status, "on_track")
        self.assertEqual(plan.paychecks_remaining, 3)
        self.assertEqual(plan.per_paycheck_cents, 10000)

    # ------------------------------------------------------------------
    # to_dict serialization
    # ------------------------------------------------------------------

    def test_to_dict_includes_formatted_fields(self):
        from app.services.savings import paycheck_plan_for_goal
        user = _user("biweekly", self.today)
        goal = _goal(target_cents=100000, deadline=self.today + timedelta(days=140))
        plan = paycheck_plan_for_goal(goal, user, on=self.today)

        d = plan.to_dict()
        self.assertIn("per_paycheck_cents", d)
        self.assertIn("per_paycheck_formatted", d)
        self.assertIn("paychecks_remaining", d)
        self.assertIn("status", d)
        self.assertEqual(d["per_paycheck_formatted"], "$100.00")


if __name__ == "__main__":
    unittest.main()
