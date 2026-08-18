"""
Budget aggregation.

Given a user and a pay period, calculate:
- expected income (paychecks received in period)
- bills due in the period (and bills already paid)
- expenses logged in the period
- savings contributed in the period
- debt paid in the period
- discretionary remaining (income - bills_due - savings - debt_paid - expenses)

This service reads from the database but doesn't mutate anything.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from app import db
from app.services.payday import Period, current_period


@dataclass
class BudgetSummary:
    """Everything the dashboard needs in one struct."""

    period: Period
    income_cents: int             # paychecks received this period
    bills_due_cents: int          # total of all bills due in period (paid + unpaid)
    bills_paid_cents: int         # bills already paid (subset of bills_due)
    bills_unpaid_cents: int       # bills still owed in period
    expenses_cents: int           # discretionary spending logged this period
    savings_cents: int            # contributions to goals this period
    debt_paid_cents: int          # debt payments this period
    discretionary_remaining_cents: int  # income - everything committed
    bills_count_unpaid: int
    bills_count_overdue: int
    upcoming_bills: list[dict]    # next 5 unpaid bills (date asc)
    active_goals: list[dict]      # goals with progress info
    active_debts: list[dict]      # debts with current balance
    recent_expenses: list[dict]   # last 5 expenses

    def to_dict(self) -> dict:
        return {
            "period": {
                "start": self.period.start.isoformat(),
                "end": self.period.end.isoformat(),
                "next_payday": self.period.next_payday.isoformat(),
                "days": self.period.days,
            },
            "income_cents": self.income_cents,
            "bills_due_cents": self.bills_due_cents,
            "bills_paid_cents": self.bills_paid_cents,
            "bills_unpaid_cents": self.bills_unpaid_cents,
            "expenses_cents": self.expenses_cents,
            "savings_cents": self.savings_cents,
            "debt_paid_cents": self.debt_paid_cents,
            "discretionary_remaining_cents": self.discretionary_remaining_cents,
            "bills_count_unpaid": self.bills_count_unpaid,
            "bills_count_overdue": self.bills_count_overdue,
            "upcoming_bills": self.upcoming_bills,
            "active_goals": self.active_goals,
            "active_debts": self.active_debts,
            "recent_expenses": self.recent_expenses,
        }


def summarize_for_user(user: dict, *, on: Optional[date] = None) -> BudgetSummary:
    """
    Build a BudgetSummary for a user, scoped to the pay period containing `on`
    (defaults to today).
    """
    today = on or date.today()
    anchor = date.fromisoformat(user["first_payday"]) \
        if isinstance(user["first_payday"], str) else user["first_payday"]
    period = current_period(anchor, user["pay_schedule"], today)
    uid = user["id"]
    start = period.start.isoformat()
    end = period.end.isoformat()

    income_cents = _sum(
        "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM paychecks "
        "WHERE user_id = ? AND received_on BETWEEN ? AND ?",
        (uid, start, end),
    )

    bills_in_period = db.query(
        """
        SELECT id, name, amount_cents, due_date, status, category, paid_on
        FROM bills
        WHERE user_id = ? AND due_date BETWEEN ? AND ?
        ORDER BY due_date ASC
        """,
        (uid, start, end),
    )
    bills_due_cents = sum(b["amount_cents"] for b in bills_in_period)
    bills_paid_cents = sum(
        b["amount_cents"] for b in bills_in_period if b["status"] == "paid"
    )
    bills_unpaid_cents = bills_due_cents - bills_paid_cents

    bills_count_unpaid = sum(
        1 for b in bills_in_period if b["status"] in ("unpaid", "overdue")
    )
    bills_count_overdue = _sum(
        "SELECT COUNT(*) AS s FROM bills WHERE user_id = ? "
        "AND status IN ('unpaid','overdue') AND due_date < ?",
        (uid, today.isoformat()),
    )

    expenses_cents = _sum(
        "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM expenses "
        "WHERE user_id = ? AND spent_on BETWEEN ? AND ?",
        (uid, start, end),
    )

    savings_cents = _sum(
        """
        SELECT COALESCE(SUM(c.amount_cents), 0) AS s
        FROM savings_contributions c
        JOIN savings_goals g ON g.id = c.goal_id
        WHERE g.user_id = ? AND c.contributed_on BETWEEN ? AND ?
        """,
        (uid, start, end),
    )

    debt_paid_cents = _sum(
        """
        SELECT COALESCE(SUM(p.amount_cents), 0) AS s
        FROM debt_payments p
        JOIN debts d ON d.id = p.debt_id
        WHERE d.user_id = ? AND p.paid_on BETWEEN ? AND ?
        """,
        (uid, start, end),
    )

    # "Discretionary remaining" = income committed elsewhere - already spent.
    # We DO NOT subtract bills_paid twice (paid bills already moved out of
    # discretionary). Instead: income - (unpaid bills + savings + debt + spent).
    # Paid bills are tracked implicitly via the expenses-like flow.
    discretionary_remaining = (
        income_cents - bills_unpaid_cents - bills_paid_cents
        - savings_cents - debt_paid_cents - expenses_cents
    )

    upcoming_bills = db.query(
        """
        SELECT id, name, amount_cents, due_date, status, category
        FROM bills
        WHERE user_id = ? AND status IN ('unpaid','overdue')
        ORDER BY due_date ASC LIMIT 5
        """,
        (uid,),
    )

    active_goals = db.query(
        """
        SELECT g.id, g.name, g.target_amount_cents, g.deadline, g.status,
               COALESCE(SUM(c.amount_cents), 0) AS saved_cents
        FROM savings_goals g
        LEFT JOIN savings_contributions c ON c.goal_id = g.id
        WHERE g.user_id = ? AND g.status = 'active'
        GROUP BY g.id
        ORDER BY g.deadline IS NULL, g.deadline ASC, g.created_at DESC
        """,
        (uid,),
    )

    active_debts = db.query(
        """
        SELECT id, name, starting_balance_cents, current_balance_cents,
               minimum_payment_cents, target_payoff_date
        FROM debts WHERE user_id = ? AND status = 'active'
        ORDER BY current_balance_cents DESC
        """,
        (uid,),
    )

    recent_expenses = db.query(
        """
        SELECT id, amount_cents, category, spent_on, description
        FROM expenses WHERE user_id = ?
        ORDER BY spent_on DESC, id DESC LIMIT 5
        """,
        (uid,),
    )

    return BudgetSummary(
        period=period,
        income_cents=income_cents,
        bills_due_cents=bills_due_cents,
        bills_paid_cents=bills_paid_cents,
        bills_unpaid_cents=bills_unpaid_cents,
        expenses_cents=expenses_cents,
        savings_cents=savings_cents,
        debt_paid_cents=debt_paid_cents,
        discretionary_remaining_cents=discretionary_remaining,
        bills_count_unpaid=bills_count_unpaid,
        bills_count_overdue=bills_count_overdue,
        upcoming_bills=upcoming_bills,
        active_goals=active_goals,
        active_debts=active_debts,
        recent_expenses=recent_expenses,
    )


def _sum(sql: str, params) -> int:
    """Run a SUM/COUNT query and return the integer (0 if NULL)."""
    row = db.query_one(sql, params)
    if row is None:
        return 0
    return int(list(row.values())[0] or 0)
