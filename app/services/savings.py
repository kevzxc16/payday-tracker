"""
Savings planning math.

Given an active goal with a deadline and a user's pay schedule, compute how
much to save per paycheck to hit the goal by the deadline.

The calculation is pure — no DB access — so it's straightforward to test in
isolation against fixed dates.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal, Optional

from app.services.money import format_cents
from app.services.payday import PaySchedule, next_payday

# Status of a per-paycheck plan.
#  - on_track:        normal case — N paychecks remaining, save $X each
#  - fully_funded:    goal already met (saved >= target)
#  - due_this_period: deadline falls before the next payday (need it all now)
#  - deadline_passed: deadline already in the past
PlanStatus = Literal["on_track", "fully_funded", "due_this_period", "deadline_passed"]


@dataclass(frozen=True)
class PaycheckPlan:
    """How much to save per paycheck to hit a goal's deadline.

    Money values are integer cents to match the rest of the codebase.
    """

    per_paycheck_cents: int
    paychecks_remaining: int
    remaining_cents: int
    status: PlanStatus

    @property
    def per_paycheck_formatted(self) -> str:
        return format_cents(self.per_paycheck_cents)

    @property
    def remaining_formatted(self) -> str:
        return format_cents(self.remaining_cents)

    def to_dict(self) -> dict:
        return {
            "per_paycheck_cents": self.per_paycheck_cents,
            "per_paycheck_formatted": self.per_paycheck_formatted,
            "paychecks_remaining": self.paychecks_remaining,
            "remaining_cents": self.remaining_cents,
            "remaining_formatted": self.remaining_formatted,
            "status": self.status,
        }


def paycheck_plan_for_goal(
    goal: dict,
    user: dict,
    *,
    on: Optional[date] = None,
) -> Optional[PaycheckPlan]:
    """
    Compute the per-paycheck savings plan for a goal.

    Returns None when no plan is computable:
      - the goal has no deadline (can't divide by zero time)
      - the goal isn't 'active' (achieved/paused/cancelled don't need a plan)

    Returns a PaycheckPlan otherwise. The `status` field encodes the four
    cases the UI needs to render distinctly. In every case except
    'on_track' the caller should display custom copy instead of the raw
    "$X per paycheck" string.

    Arguments:
      goal: row from savings_goals augmented with 'saved_cents'
            (the sum of contributions)
      user: row from users (must contain 'pay_schedule' and 'first_payday')
      on:   reference date — defaults to today. Passed by tests for
            deterministic output.
    """
    if goal.get("status") and goal["status"] != "active":
        return None
    if not goal.get("deadline"):
        return None

    today = on or date.today()
    deadline = _to_date(goal["deadline"])
    saved = int(goal.get("saved_cents") or 0)
    target = int(goal["target_amount_cents"])
    remaining = max(0, target - saved)

    # Already there — no plan needed beyond "you're done".
    if remaining == 0:
        return PaycheckPlan(
            per_paycheck_cents=0,
            paychecks_remaining=0,
            remaining_cents=0,
            status="fully_funded",
        )

    # Deadline in the past — surface that explicitly. The user owes the
    # whole remaining amount with zero scheduled paychecks to spread it.
    if deadline < today:
        return PaycheckPlan(
            per_paycheck_cents=remaining,
            paychecks_remaining=0,
            remaining_cents=remaining,
            status="deadline_passed",
        )

    anchor = _to_date(user["first_payday"])
    schedule: PaySchedule = user["pay_schedule"]

    # Count paydays strictly after today and on-or-before the deadline.
    # next_payday is strict-greater-than, so we use today directly as the
    # cursor and walk forward.
    paydays = _count_paydays_in_range(anchor, schedule, today, deadline)

    if paydays == 0:
        return PaycheckPlan(
            per_paycheck_cents=remaining,
            paychecks_remaining=0,
            remaining_cents=remaining,
            status="due_this_period",
        )

    # Ceiling division so the user actually hits (or slightly exceeds) the
    # target; flooring would leave a few cents short on uneven splits.
    per_paycheck = (remaining + paydays - 1) // paydays
    return PaycheckPlan(
        per_paycheck_cents=per_paycheck,
        paychecks_remaining=paydays,
        remaining_cents=remaining,
        status="on_track",
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _to_date(value) -> date:
    """Accept either a date or an ISO date string; return a date."""
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _count_paydays_in_range(
    anchor: date,
    schedule: PaySchedule,
    after: date,
    until: date,
    *,
    max_iterations: int = 2000,
) -> int:
    """Count paydays strictly after `after` and on-or-before `until`.

    Walks forward one payday at a time using next_payday(). The
    `max_iterations` cap is paranoia against pathological dates — at 2000
    biweekly paydays that's ~76 years, well past any reasonable goal.
    """
    count = 0
    cursor = after
    for _ in range(max_iterations):
        cursor = next_payday(anchor, schedule, cursor)
        if cursor > until:
            return count
        count += 1
    return count
