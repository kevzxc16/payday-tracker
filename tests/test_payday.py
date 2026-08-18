"""Tests for app.services.payday — pay-period math."""
import unittest
from datetime import date, timedelta

from app.services.payday import (
    Period, current_period, next_payday, previous_payday,
)


class WeeklyTests(unittest.TestCase):
    """Weekly: same weekday every 7 days."""

    anchor = date(2026, 6, 12)  # a Friday

    def test_next_strictly_after(self):
        # Asking on the anchor itself → next Friday
        self.assertEqual(next_payday(self.anchor, "weekly", self.anchor),
                         self.anchor + timedelta(days=7))

    def test_next_mid_week(self):
        wednesday = self.anchor + timedelta(days=5)
        self.assertEqual(next_payday(self.anchor, "weekly", wednesday),
                         self.anchor + timedelta(days=7))

    def test_previous_on_anchor(self):
        # Asking previous on anchor → the anchor itself (inclusive)
        self.assertEqual(previous_payday(self.anchor, "weekly", self.anchor),
                         self.anchor)


class BiweeklyTests(unittest.TestCase):
    """Biweekly: same weekday every 14 days."""

    anchor = date(2026, 1, 2)  # arbitrary Friday

    def test_current_period_spans_two_weeks(self):
        # 5 days after anchor — still in the same period
        on = self.anchor + timedelta(days=5)
        p = current_period(self.anchor, "biweekly", on)
        self.assertEqual(p.start, self.anchor)
        self.assertEqual(p.next_payday, self.anchor + timedelta(days=14))
        self.assertEqual(p.days, 14)

    def test_period_after_many_cycles(self):
        # 100 days after anchor
        on = self.anchor + timedelta(days=100)
        p = current_period(self.anchor, "biweekly", on)
        # Period start should be a multiple-of-14 days from anchor
        delta = (p.start - self.anchor).days
        self.assertEqual(delta % 14, 0)
        # And `on` should fall in [start, end].
        self.assertTrue(p.start <= on <= p.end)


class MonthlyTests(unittest.TestCase):
    """Monthly: same day of month, with end-of-month clamping."""

    def test_clamp_jan_31_to_feb_28(self):
        anchor = date(2026, 1, 31)
        # 2026 isn't a leap year; Feb 28 is the closest day to "the 31st"
        self.assertEqual(next_payday(anchor, "monthly", anchor),
                         date(2026, 2, 28))

    def test_clamp_then_unclamp(self):
        anchor = date(2026, 1, 31)
        # Feb → Mar: should jump back to Mar 31, not stay clamped at 28
        nxt = next_payday(anchor, "monthly", date(2026, 2, 28))
        self.assertEqual(nxt, date(2026, 3, 31))

    def test_previous_monthly(self):
        anchor = date(2026, 1, 15)
        self.assertEqual(previous_payday(anchor, "monthly", date(2026, 4, 17)),
                         date(2026, 4, 15))


class FutureAnchorTests(unittest.TestCase):
    """An anchor in the future should still let us reason about today."""

    def test_biweekly_future_anchor(self):
        future = date(2027, 1, 1)
        # 2026-12-15 is before the anchor; we want the most recent past payday
        # which is 28 days before (= 2026-12-04).
        p = current_period(future, "biweekly", date(2026, 12, 15))
        self.assertEqual(p.start, date(2026, 12, 4))
        self.assertEqual(p.next_payday, date(2026, 12, 18))


class InvalidScheduleTests(unittest.TestCase):
    def test_unknown_schedule_raises(self):
        with self.assertRaises(ValueError):
            next_payday(date(2026, 1, 1), "annually", date(2026, 6, 1))


if __name__ == "__main__":
    unittest.main()
