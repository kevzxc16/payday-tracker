"""
Dashboard route — aggregates the budget summary for the current pay period.

HTML at /dashboard. JSON API at /api/v1/dashboard.
"""
from __future__ import annotations

from app.http_utils import Request, Response, html, json_response, redirect
from app.middleware import auth_required
from app.router import router
from app.services.budget import summarize_for_user
from app.services.money import format_cents
from app.services.savings import paycheck_plan_for_goal
from app.templating import e, nav_html, render


@router.route("GET", "/")
def index(req: Request) -> Response:
    """Root URL: logged-in users → dashboard, everyone else → landing page."""
    if req.user is not None:
        return redirect("/dashboard")
    return html(render(
        "landing.html",
        _layout="marketing_base.html",
        title="Payday Tracker — Budgeting for hourly workers",
        meta_description=(
            "A simple budgeting app for people paid weekly or biweekly. "
            "Know exactly what's safe to spend before your next paycheck — "
            "without guilt or panic. Free during beta."
        ),
    ))


@router.route("GET", "/dashboard")
@auth_required
def dashboard(req: Request) -> Response:
    from datetime import date as _date
    summary = summarize_for_user(req.user)
    csrf = e(req.session["csrf_token"])

    # Position of "today" within the current pay period for the signature
    # progress track. Clamped to [0, 100] for safety even if the user's
    # anchor produces an edge-case period.
    today = _date.today()
    span_days = max(1, (summary.period.next_payday - summary.period.start).days)
    elapsed = max(0, (today - summary.period.start).days)
    period_progress_pct = max(0, min(100, round(100 * elapsed / span_days, 1)))
    days_to_payday = max(0, (summary.period.next_payday - today).days)

    # Build the upcoming-bills list, grouped by next-payday boundary so the
    # user can see what's coming due BEFORE their next deposit lands — the
    # actual question they care about.
    if summary.upcoming_bills:
        from datetime import date as _d
        next_payday = summary.period.next_payday
        before, after = [], []
        for b in summary.upcoming_bills:
            due = b["due_date"]
            if isinstance(due, str):
                due = _d.fromisoformat(due)
            (before if due < next_payday else after).append(b)

        def _li(b):
            return (
                f'<li>'
                f'<span class="muted">{e(str(b["due_date"]))}</span> '
                f'<strong>{e(b["name"])}</strong> '
                f'<span class="amount">{e(format_cents(b["amount_cents"]))}</span>'
                f'</li>'
            )

        def _mini_header(label, total_cents):
            return (
                f'<li class="group-row">'
                f'<span class="group-label-small">{e(label)}</span>'
                f'<span class="group-total-small amount">'
                f'{e(format_cents(total_cents))}</span>'
                f'</li>'
            )

        parts: list[str] = []
        if before:
            parts.append(_mini_header(
                "Before next payday",
                sum(b["amount_cents"] for b in before),
            ))
            parts.extend(_li(b) for b in before)
        if after:
            parts.append(_mini_header(
                "After next payday",
                sum(b["amount_cents"] for b in after),
            ))
            parts.extend(_li(b) for b in after)
        upcoming_rows = "".join(parts)
    else:
        upcoming_rows = '<li class="muted">No upcoming bills 🎉</li>'

    # Goals list with progress bars.
    if summary.active_goals:
        goal_rows = []
        for g in summary.active_goals:
            saved = int(g["saved_cents"])
            target = int(g["target_amount_cents"])
            pct = min(100, round(100 * saved / target)) if target else 0

            # Compact per-paycheck plan, only for active goals with a
            # deadline. The savings page carries the full sentence; here
            # we just show a small tag so the panel stays scannable.
            plan = paycheck_plan_for_goal(g, req.user)
            plan_tag = ""
            if plan is not None:
                if plan.status == "on_track":
                    plan_tag = (
                        f' <span class="tag tag-blue">'
                        f'{e(plan.per_paycheck_formatted)}/pay'
                        f'</span>'
                    )
                elif plan.status == "due_this_period":
                    plan_tag = ' <span class="tag tag-yellow">due now</span>'
                elif plan.status == "deadline_passed":
                    plan_tag = ' <span class="tag tag-red">overdue</span>'
                # fully_funded → no tag; status already shows on savings page

            goal_rows.append(
                f'<li>'
                f'<div class="goal-mini-header">'
                f'<strong>{e(g["name"])}</strong>{plan_tag} '
                f'<span class="muted">{e(format_cents(saved))} / {e(format_cents(target))}</span>'
                f'</div>'
                f'<div class="progress-bar small"><div class="progress-fill" style="width:{pct}%"></div></div>'
                f'</li>'
            )
        goals_html = "".join(goal_rows)
    else:
        goals_html = '<li class="muted">No active savings goals.</li>'

    # Active debts.
    if summary.active_debts:
        debt_rows = []
        for d in summary.active_debts:
            start = int(d["starting_balance_cents"])
            cur = int(d["current_balance_cents"])
            paid = start - cur
            pct = min(100, round(100 * paid / start)) if start else 0
            debt_rows.append(
                f'<li>'
                f'<div class="goal-mini-header">'
                f'<strong>{e(d["name"])}</strong> '
                f'<span class="muted">{e(format_cents(cur))} remaining</span>'
                f'</div>'
                f'<div class="progress-bar small"><div class="progress-fill green" style="width:{pct}%"></div></div>'
                f'</li>'
            )
        debts_html = "".join(debt_rows)
    else:
        debts_html = '<li class="muted">No active debts.</li>'

    # Recent expenses.
    if summary.recent_expenses:
        recent_rows = "".join(
            f'<li>'
            f'<span class="muted">{e(str(x["spent_on"]))}</span> '
            f'<strong>{e(x["category"])}</strong>'
            + (f' — {e(x["description"])}' if x.get("description") else "")
            + f' <span class="amount">{e(format_cents(x["amount_cents"]))}</span>'
            f'</li>'
            for x in summary.recent_expenses
        )
    else:
        recent_rows = '<li class="muted">No spending logged yet.</li>'

    discretionary_class = "kpi-good" if summary.discretionary_remaining_cents >= 0 else "kpi-bad"
    overdue_warning = ""
    if summary.bills_count_overdue:
        overdue_warning = (
            f'<div class="alert alert-error">'
            f'⚠ You have {summary.bills_count_overdue} overdue bill'
            f'{"s" if summary.bills_count_overdue != 1 else ""}.'
            f'</div>'
        )

    return html(render(
        "dashboard.html",
        title="Dashboard",
        nav=nav_html("dashboard"),
        csrf_token=csrf,
        user_email=e(req.user["email"]),
        period_start=e(summary.period.start.isoformat()),
        period_end=e(summary.period.end.isoformat()),
        next_payday=e(summary.period.next_payday.isoformat()),
        period_days=summary.period.days,
        period_progress_pct=period_progress_pct,
        days_to_payday=days_to_payday,
        income_amount=e(format_cents(summary.income_cents)),
        bills_unpaid_amount=e(format_cents(summary.bills_unpaid_cents)),
        bills_paid_amount=e(format_cents(summary.bills_paid_cents)),
        bills_count_unpaid=summary.bills_count_unpaid,
        expenses_amount=e(format_cents(summary.expenses_cents)),
        savings_amount=e(format_cents(summary.savings_cents)),
        debt_paid_amount=e(format_cents(summary.debt_paid_cents)),
        discretionary_amount=e(format_cents(summary.discretionary_remaining_cents)),
        discretionary_class=discretionary_class,
        overdue_warning=overdue_warning,
        upcoming_bills_html=upcoming_rows,
        goals_html=goals_html,
        debts_html=debts_html,
        recent_expenses_html=recent_rows,
    ))


@router.route("GET", "/api/v1/dashboard")
@auth_required
def api_dashboard(req: Request) -> Response:
    """Same data as the HTML dashboard, just JSON."""
    summary = summarize_for_user(req.user)
    return json_response(summary.to_dict())
