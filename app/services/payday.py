"""
Payday calculator.

Given a user's pay schedule ('weekly' | 'biweekly' | 'monthly') and a known
anchor payday ('first_payday'), figure out:
- the current pay period [period_start, period_end] for any given date
- the next payday after a given date
- the previous payday at or before a given date

These functions take plain dates and pay schedules — no DB. They're pure and
easy to test.
"""
from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

PaySchedule = Literal["weekly", "biweekly", "monthly"]


@dataclass(frozen=True)
class Period:
    """A pay period — funds become available on `start`, run out on `end`."""

    start: date  # payday: money becomes available
    end: date    # day before the next payday (inclusive)
    next_payday: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


def next_payday(anchor: date, schedule: PaySchedule, after: date) -> date:
    """
    Find the first payday strictly after `after`, given the anchor and schedule.

    `anchor` is any known payday. We walk forward (or backward) from it.
    """
    if schedule == "weekly":
        return _next_periodic(anchor, after, timedelta(days=7))
    if schedule == "biweekly":
        return _next_periodic(anchor, after, timedelta(days=14))
    if schedule == "monthly":
        return _next_monthly(anchor, after)
    raise ValueError(f"unknown pay schedule: {schedule!r}")


def previous_payday(anchor: date, schedule: PaySchedule, on_or_before: date) -> date:
    """
    Find the most recent payday on or before `on_or_before`.

    If `on_or_before` itself is a payday, returns it.
    """
    if schedule == "weekly":
        return _previous_periodic(anchor, on_or_before, timedelta(days=7))
    if schedule == "biweekly":
        return _previous_periodic(anchor, on_or_before, timedelta(days=14))
    if schedule == "monthly":
        return _previous_monthly(anchor, on_or_before)
    raise ValueError(f"unknown pay schedule: {schedule!r}")


def current_period(anchor: date, schedule: PaySchedule, on: date) -> Period:
    """
    Return the pay period containing `on`.

    period_start = previous_payday(on)
    next_payday  = next_payday(on)
    period_end   = next_payday - 1 day
    """
    start = previous_payday(anchor, schedule, on)
    nxt = next_payday(anchor, schedule, start)
    return Period(start=start, end=nxt - timedelta(days=1), next_payday=nxt)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _next_periodic(anchor: date, after: date, step: timedelta) -> date:
    """Next anchor+k*step that is strictly > after."""
    delta_days = (after - anchor).days
    step_days = step.days
    # Number of whole periods elapsed since the anchor.
    periods_elapsed = delta_days // step_days
    candidate = anchor + step * periods_elapsed
    while candidate <= after:
        candidate += step
    return candidate


def _previous_periodic(anchor: date, on_or_before: date, step: timedelta) -> date:
    """Most recent anchor+k*step that is <= on_or_before."""
    delta_days = (on_or_before - anchor).days
    step_days = step.days
    periods_elapsed = delta_days // step_days
    return anchor + step * periods_elapsed


def _add_months(d: date, months: int) -> date:
    """
    Add (or subtract) calendar months to a date, clamping to month length.

    Adding 1 month to Jan 31 yields Feb 28/29.
    """
    new_month_zero_index = d.month - 1 + months
    new_year = d.year + new_month_zero_index // 12
    new_month = new_month_zero_index % 12 + 1
    last_day_of_new_month = monthrange(new_year, new_month)[1]
    new_day = min(d.day, last_day_of_new_month)
    return date(new_year, new_month, new_day)


def _next_monthly(anchor: date, after: date) -> date:
    """Next monthly anchor strictly after `after`. Handles month-end clamping."""
    # Estimate the months delta, then walk forward to satisfy strict >.
    months_delta = (after.year - anchor.year) * 12 + (after.month - anchor.month)
    candidate = _add_months(anchor, months_delta)
    while candidate <= after:
        months_delta += 1
        candidate = _add_months(anchor, months_delta)
    return candidate


def _previous_monthly(anchor: date, on_or_before: date) -> date:
    """Most recent monthly anchor on or before `on_or_before`."""
    months_delta = (on_or_before.year - anchor.year) * 12 + (on_or_before.month - anchor.month)
    candidate = _add_months(anchor, months_delta)
    while candidate > on_or_before:
        months_delta -= 1
        candidate = _add_months(anchor, months_delta)
    return candidate
