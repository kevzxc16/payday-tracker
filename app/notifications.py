"""
Notifications service.

Two responsibilities:

1. **Enqueue** notifications by writing pending rows to the `notifications`
   table. The scheduler picks them up later.

2. **Generate** notifications based on the user's data — bills coming due,
   payday reminders, debt due dates, period check-ins. Generation is
   idempotent: calling it twice doesn't create duplicate notifications for
   the same reference.

Notifications are delivery-channel-agnostic at this layer. Phase 3 ships
email; future tiers may add push or in-app banners.

Notification types:
- bill_due        — bill is due in <=3 days
- savings_reminder — payday encouragement to contribute
- debt_due        — minimum payment due (uses target_payoff_date as proxy)
- period_checkin  — end of pay period reflection
- welcome         — sent on signup
- password_reset  — for forgot-password flow (used by auth module)
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from app import db
from app.config import settings
from app.services.budget import summarize_for_user
from app.services.money import format_cents
from app.services.payday import current_period

# How many days before a bill's due date to send a reminder.
BILL_REMINDER_LEAD_DAYS = 3

# Notification type → allowed types in the DB (matches CHECK constraint).
VALID_TYPES = {
    "bill_due", "savings_reminder", "debt_due", "period_checkin",
    "password_reset", "welcome",
}


# ---------------------------------------------------------------------------
# Enqueue helpers
# ---------------------------------------------------------------------------

def enqueue(
    user_id: int,
    type: str,
    subject: str,
    body: str,
    *,
    scheduled_for: Optional[datetime] = None,
    reference_type: Optional[str] = None,
    reference_id: Optional[int] = None,
) -> int:
    """
    Write a pending notification row. `scheduled_for` defaults to now (= send
    on next scheduler tick).
    """
    if type not in VALID_TYPES:
        raise ValueError(f"unknown notification type: {type!r}")
    when = (scheduled_for or datetime.utcnow()).strftime("%Y-%m-%d %H:%M:%S")
    cur = db.execute(
        """
        INSERT INTO notifications
            (user_id, type, subject, body, scheduled_for, status,
             reference_type, reference_id)
        VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
        """,
        (user_id, type, subject, body, when, reference_type, reference_id),
    )
    return cur.lastrowid


def list_for_user(user_id: int, *, limit: int = 50) -> list[dict]:
    return db.query(
        """
        SELECT id, type, subject, body, scheduled_for, sent_at, status,
               last_error, reference_type, reference_id, created_at
        FROM notifications
        WHERE user_id = ?
        ORDER BY id DESC LIMIT ?
        """,
        (user_id, limit),
    )


def cancel(user_id: int, notif_id: int) -> bool:
    """Mark a pending notification as cancelled. Returns True if it changed."""
    cur = db.execute(
        """
        UPDATE notifications SET status = 'cancelled'
        WHERE id = ? AND user_id = ? AND status = 'pending'
        """,
        (notif_id, user_id),
    )
    return cur.rowcount > 0


def dispatch_pending(now: Optional[datetime] = None) -> dict:
    """
    Look at all pending notifications that are due (scheduled_for <= now).
    Send each via the configured channel. Returns a small summary dict.

    This is the function the scheduler calls every tick.
    """
    from app.email_sender import send_email  # imported here to avoid cycles

    when = (now or datetime.utcnow()).strftime("%Y-%m-%d %H:%M:%S")
    pending = db.query(
        """
        SELECT n.id, n.user_id, n.type, n.subject, n.body, u.email
        FROM notifications n
        JOIN users u ON u.id = n.user_id
        WHERE n.status = 'pending' AND n.scheduled_for <= ?
        ORDER BY n.scheduled_for ASC, n.id ASC
        LIMIT 100
        """,
        (when,),
    )

    sent = 0
    failed = 0
    for row in pending:
        try:
            send_email(row["email"], row["subject"], row["body"])
            db.execute(
                "UPDATE notifications SET status = 'sent', sent_at = ?, "
                "last_error = NULL WHERE id = ?",
                (when, row["id"]),
            )
            sent += 1
        except Exception as exc:  # noqa: BLE001 — we log + record then continue
            err = str(exc)[:500]
            db.execute(
                "UPDATE notifications SET status = 'failed', last_error = ? "
                "WHERE id = ?",
                (err, row["id"]),
            )
            failed += 1
    return {"checked": len(pending), "sent": sent, "failed": failed}


# ---------------------------------------------------------------------------
# Generation rules — read user data, enqueue notifications as appropriate.
# Idempotent: don't enqueue a duplicate pending notification for the same
# (type, reference_type, reference_id, user_id) combination.
# ---------------------------------------------------------------------------

def generate_for_user(user: dict, *, on: Optional[date] = None) -> dict:
    """
    Run all generation rules for a single user. Returns a summary count.
    """
    today = on or date.today()
    created = {
        "bill_due": 0,
        "savings_reminder": 0,
        "debt_due": 0,
        "period_checkin": 0,
    }
    created["bill_due"] += _generate_bill_due(user, today)
    created["savings_reminder"] += _generate_savings_reminder(user, today)
    created["debt_due"] += _generate_debt_due(user, today)
    created["period_checkin"] += _generate_period_checkin(user, today)
    return created


def generate_for_all_users(*, on: Optional[date] = None) -> dict:
    """Run generation for every user. Used by the scheduler."""
    users = db.query("SELECT id, email, pay_schedule, first_payday FROM users")
    totals = {"bill_due": 0, "savings_reminder": 0, "debt_due": 0, "period_checkin": 0}
    for u in users:
        # Need the full user row for budget summarization.
        full = db.query_one("SELECT * FROM users WHERE id = ?", (u["id"],))
        if full is None:
            continue
        out = generate_for_user(full, on=on)
        for k, v in out.items():
            totals[k] += v
    return totals


# ---------------------------------------------------------------------------
# Individual generation rules
# ---------------------------------------------------------------------------

def _already_pending(user_id: int, type: str, reference_type: str,
                     reference_id: int) -> bool:
    """True if a pending (or already-sent today) notification exists for this ref."""
    row = db.query_one(
        """
        SELECT id FROM notifications
        WHERE user_id = ? AND type = ?
          AND reference_type = ? AND reference_id = ?
          AND status IN ('pending', 'sent')
        ORDER BY id DESC LIMIT 1
        """,
        (user_id, type, reference_type, reference_id),
    )
    return row is not None


def _generate_bill_due(user: dict, today: date) -> int:
    """
    For each unpaid bill due within BILL_REMINDER_LEAD_DAYS, enqueue a reminder
    (if one doesn't already exist for that bill).
    """
    cutoff = today + timedelta(days=BILL_REMINDER_LEAD_DAYS)
    rows = db.query(
        """
        SELECT id, name, amount_cents, due_date FROM bills
        WHERE user_id = ? AND status IN ('unpaid', 'overdue')
          AND due_date <= ?
        """,
        (user["id"], cutoff.isoformat()),
    )
    count = 0
    for b in rows:
        if _already_pending(user["id"], "bill_due", "bills", b["id"]):
            continue
        due = b["due_date"]
        amount = format_cents(b["amount_cents"])
        subject = f"Bill reminder: {b['name']} due {due}"
        body = (
            f"Hi,\n\n"
            f"Your bill \"{b['name']}\" for {amount} is due on {due}.\n\n"
            f"Mark it paid here: {settings.BASE_URL}/bills\n\n"
            f"— Payday Tracker"
        )
        enqueue(
            user["id"], "bill_due", subject, body,
            reference_type="bills", reference_id=b["id"],
        )
        count += 1
    return count


def _generate_savings_reminder(user: dict, today: date) -> int:
    """If today is the user's payday, enqueue a 'save some of this' nudge."""
    try:
        anchor = date.fromisoformat(user["first_payday"]) \
            if isinstance(user["first_payday"], str) else user["first_payday"]
        period = current_period(anchor, user["pay_schedule"], today)
    except (ValueError, TypeError):
        return 0
    if period.start != today:
        return 0
    # Reference id = days since epoch — unique per payday so we don't double-send.
    ref_id = (today - date(1970, 1, 1)).days
    if _already_pending(user["id"], "savings_reminder", "paydays", ref_id):
        return 0
    # Skip if the user has no active goals — nothing to contribute to.
    goals = db.query_one(
        "SELECT COUNT(*) AS n FROM savings_goals WHERE user_id = ? AND status = 'active'",
        (user["id"],),
    )
    if (goals or {}).get("n", 0) == 0:
        return 0
    subject = "Payday — set aside some savings?"
    body = (
        f"Happy payday!\n\n"
        f"Now's a great time to contribute to your savings goals before "
        f"the money disappears into other things.\n\n"
        f"Visit your goals here: {settings.BASE_URL}/savings\n\n"
        f"— Payday Tracker"
    )
    enqueue(user["id"], "savings_reminder", subject, body,
            reference_type="paydays", reference_id=ref_id)
    return 1


def _generate_debt_due(user: dict, today: date) -> int:
    """For each active debt with a target_payoff_date within 7 days, nudge."""
    cutoff = today + timedelta(days=7)
    rows = db.query(
        """
        SELECT id, name, current_balance_cents, minimum_payment_cents,
               target_payoff_date
        FROM debts
        WHERE user_id = ? AND status = 'active'
          AND target_payoff_date IS NOT NULL
          AND target_payoff_date <= ?
        """,
        (user["id"], cutoff.isoformat()),
    )
    count = 0
    for d in rows:
        if _already_pending(user["id"], "debt_due", "debts", d["id"]):
            continue
        subject = f"Debt payment due: {d['name']}"
        body = (
            f"Hi,\n\n"
            f"Your debt \"{d['name']}\" has a target payoff date of "
            f"{d['target_payoff_date']}.\n"
            f"Current balance: {format_cents(d['current_balance_cents'])}\n"
            f"Minimum payment: {format_cents(d['minimum_payment_cents'])}\n\n"
            f"Record a payment: {settings.BASE_URL}/debts\n\n"
            f"— Payday Tracker"
        )
        enqueue(user["id"], "debt_due", subject, body,
                reference_type="debts", reference_id=d["id"])
        count += 1
    return count


def _generate_period_checkin(user: dict, today: date) -> int:
    """On the last day of a pay period, send a reflection prompt."""
    try:
        anchor = date.fromisoformat(user["first_payday"]) \
            if isinstance(user["first_payday"], str) else user["first_payday"]
        period = current_period(anchor, user["pay_schedule"], today)
    except (ValueError, TypeError):
        return 0
    if period.end != today:
        return 0
    ref_id = (period.start - date(1970, 1, 1)).days
    if _already_pending(user["id"], "period_checkin", "periods", ref_id):
        return 0

    summary = summarize_for_user(user, on=today)
    subject = "Pay period check-in"
    body = (
        f"Hi,\n\n"
        f"Here's your snapshot for the period {period.start} → {period.end}:\n\n"
        f"  Income:          {format_cents(summary.income_cents)}\n"
        f"  Bills paid:      {format_cents(summary.bills_paid_cents)}\n"
        f"  Bills unpaid:    {format_cents(summary.bills_unpaid_cents)}\n"
        f"  Spending:        {format_cents(summary.expenses_cents)}\n"
        f"  Saved:           {format_cents(summary.savings_cents)}\n"
        f"  Debt paid:       {format_cents(summary.debt_paid_cents)}\n"
        f"  Discretionary:   {format_cents(summary.discretionary_remaining_cents)}\n\n"
        f"Next payday: {period.next_payday}\n\n"
        f"How did this period go for you? What's one thing you'd do "
        f"differently next period?\n\n"
        f"View the dashboard: {settings.BASE_URL}/dashboard\n\n"
        f"— Payday Tracker"
    )
    enqueue(user["id"], "period_checkin", subject, body,
            reference_type="periods", reference_id=ref_id)
    return 1


def send_welcome(user_id: int, email: str) -> None:
    """Called from signup to queue an immediate welcome email."""
    subject = "Welcome to Payday Tracker"
    body = (
        f"Hi,\n\n"
        f"Welcome to Payday Tracker — a simple way to plan your money\n"
        f"between paychecks.\n\n"
        f"To get started:\n"
        f"  1. Record your most recent paycheck: {settings.BASE_URL}/income\n"
        f"  2. Add the bills you owe this period: {settings.BASE_URL}/bills\n"
        f"  3. Set a savings goal: {settings.BASE_URL}/savings\n\n"
        f"Your dashboard: {settings.BASE_URL}/dashboard\n\n"
        f"— Payday Tracker"
    )
    enqueue(user_id, "welcome", subject, body)
