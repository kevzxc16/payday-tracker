"""
Savings routes — goals (named targets) plus contributions toward them.

HTML at /savings, JSON API at /api/v1/savings/goals/*.
"""
from __future__ import annotations

from datetime import date

from app import db
from app.http_utils import HTTPError, Request, Response, html, json_response, redirect
from app.middleware import auth_required, csrf_protect
from app.router import router
from app.services.money import format_cents, parse_dollars
from app.services.progress import log_event
from app.services.savings import paycheck_plan_for_goal
from app.templating import e, error_block, nav_html, render

VALID_STATUSES = {"active", "achieved", "paused", "cancelled"}


def _list_goals(user_id: int) -> list[dict]:
    """Goals with computed progress (saved_cents)."""
    return db.query(
        """
        SELECT g.id, g.name, g.target_amount_cents, g.deadline, g.status, g.notes,
               COALESCE(SUM(c.amount_cents), 0) AS saved_cents
        FROM savings_goals g
        LEFT JOIN savings_contributions c ON c.goal_id = g.id
        WHERE g.user_id = ?
        GROUP BY g.id
        ORDER BY g.status = 'active' DESC, g.deadline IS NULL, g.deadline ASC
        """,
        (user_id,),
    )


def _get_goal(user_id: int, goal_id: int) -> dict:
    row = db.query_one(
        """
        SELECT g.*, COALESCE((SELECT SUM(amount_cents) FROM savings_contributions
                              WHERE goal_id = g.id), 0) AS saved_cents
        FROM savings_goals g WHERE g.id = ? AND g.user_id = ?
        """,
        (goal_id, user_id),
    )
    if row is None:
        raise HTTPError(404, "Goal not found")
    return row


def _list_contributions(goal_id: int) -> list[dict]:
    return db.query(
        """
        SELECT id, amount_cents, contributed_on, note, created_at
        FROM savings_contributions WHERE goal_id = ?
        ORDER BY contributed_on DESC, id DESC
        """,
        (goal_id,),
    )


def _validate_goal(fields: dict, *, partial: bool = False) -> dict:
    out: dict = {}

    def need(k):
        return k in fields or not partial

    if need("name"):
        name = (fields.get("name") or "").strip()
        if not name:
            raise HTTPError(400, "Goal name is required.")
        if len(name) > 120:
            raise HTTPError(400, "Name too long (max 120 chars).")
        out["name"] = name

    if need("target_amount_cents") or "target_amount" in fields:
        if "target_amount_cents" in fields:
            try:
                cents = int(fields["target_amount_cents"])
            except (TypeError, ValueError):
                raise HTTPError(400, "Invalid target amount.")
        else:
            try:
                cents = parse_dollars(fields.get("target_amount", ""))
            except ValueError as exc:
                raise HTTPError(400, str(exc))
        if cents <= 0:
            raise HTTPError(400, "Target must be positive.")
        out["target_amount_cents"] = cents

    if "deadline" in fields or not partial:
        raw = (fields.get("deadline") or "").strip() or None
        if raw is not None:
            try:
                date.fromisoformat(raw)
            except ValueError:
                raise HTTPError(400, "Invalid deadline.")
        out["deadline"] = raw

    if "status" in fields:
        st = (fields.get("status") or "").strip().lower()
        if st not in VALID_STATUSES:
            raise HTTPError(400, "Invalid status.")
        out["status"] = st

    if "notes" in fields or not partial:
        notes = (fields.get("notes") or "").strip() or None
        out["notes"] = notes

    return out


def _validate_contribution(fields: dict) -> dict:
    out: dict = {}
    if "amount_cents" in fields:
        try:
            cents = int(fields["amount_cents"])
        except (TypeError, ValueError):
            raise HTTPError(400, "Invalid amount.")
    else:
        try:
            cents = parse_dollars(fields.get("amount", ""))
        except ValueError as exc:
            raise HTTPError(400, str(exc))
    if cents <= 0:
        raise HTTPError(400, "Contribution must be positive.")
    out["amount_cents"] = cents

    raw = (fields.get("contributed_on") or date.today().isoformat()).strip()
    try:
        date.fromisoformat(raw)
    except ValueError:
        raise HTTPError(400, "Invalid contribution date.")
    out["contributed_on"] = raw

    out["note"] = (fields.get("note") or "").strip() or None
    return out


def _serialize_goal(row: dict, contributions: list[dict] | None = None,
                    *, user: dict | None = None) -> dict:
    saved = int(row.get("saved_cents") or 0)
    target = int(row["target_amount_cents"])
    pct = round(100 * saved / target, 1) if target else 0.0
    data = {
        "id": row["id"],
        "name": row["name"],
        "target_amount_cents": target,
        "target_amount_formatted": format_cents(target),
        "saved_cents": saved,
        "saved_formatted": format_cents(saved),
        "remaining_cents": max(0, target - saved),
        "remaining_formatted": format_cents(max(0, target - saved)),
        "progress_pct": pct,
        "deadline": str(row["deadline"]) if row.get("deadline") else None,
        "status": row["status"],
        "notes": row.get("notes"),
    }
    if user is not None:
        plan = paycheck_plan_for_goal(row, user)
        data["paycheck_plan"] = plan.to_dict() if plan else None
    if contributions is not None:
        data["contributions"] = [
            {
                "id": c["id"],
                "amount_cents": c["amount_cents"],
                "amount_formatted": format_cents(c["amount_cents"]),
                "contributed_on": str(c["contributed_on"]),
                "note": c.get("note"),
            } for c in contributions
        ]
    return data


def _plan_line_html(goal: dict, user: dict) -> str:
    """Render a single line of HTML describing the per-paycheck plan.

    Returns "" when no plan is computable (no deadline, or non-active goal).
    The line is meant to live directly under the progress bar inside each
    goal card.
    """
    plan = paycheck_plan_for_goal(goal, user)
    if plan is None:
        return ""

    deadline_str = e(str(goal["deadline"])) if goal.get("deadline") else ""

    if plan.status == "fully_funded":
        return (
            '<p class="plan-line plan-funded">'
            '<span class="tag tag-green">✓ Funded</span>'
            '</p>'
        )

    if plan.status == "deadline_passed":
        return (
            f'<p class="plan-line plan-overdue">'
            f'<span class="tag tag-red">Deadline passed</span> '
            f'<span class="muted">{e(format_cents(plan.remaining_cents))} '
            f'still needed</span>'
            f'</p>'
        )

    if plan.status == "due_this_period":
        return (
            f'<p class="plan-line plan-due-now">'
            f'<span class="tag tag-yellow">Due this period</span> '
            f'<span class="muted">save '
            f'<strong>{e(format_cents(plan.remaining_cents))}</strong> '
            f'before {deadline_str}</span>'
            f'</p>'
        )

    # on_track
    paychecks = plan.paychecks_remaining
    paychecks_word = "paycheck" if paychecks == 1 else "paychecks"
    return (
        f'<p class="plan-line">'
        f'Save <strong>{e(plan.per_paycheck_formatted)}</strong> per paycheck '
        f'<span class="muted">— {paychecks} {paychecks_word} until '
        f'{deadline_str}</span>'
        f'</p>'
    )



    """If saved >= target, flip status to 'achieved' and log it."""
    row = _get_goal(user_id, goal_id)
    if row["status"] != "active":
        return
    if row["saved_cents"] >= row["target_amount_cents"]:
        db.execute(
            "UPDATE savings_goals SET status = 'achieved', "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
            (goal_id, user_id),
        )
        log_event(user_id, "savings.goal_achieved",
                  {"id": goal_id, "name": row["name"]})


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

@router.route("GET", "/savings")
@auth_required
def page(req: Request) -> Response:
    return _render(req)


@router.route("POST", "/savings")
@auth_required
@csrf_protect
def create_goal_html(req: Request) -> Response:
    try:
        clean = _validate_goal(req.form())
    except HTTPError as exc:
        return _render(req, error=exc.message, status=exc.status)
    _insert_goal(req.user["id"], clean)
    return redirect("/savings")


@router.route("POST", "/savings/<int:goal_id>/contribute")
@auth_required
@csrf_protect
def contribute_html(req: Request) -> Response:
    try:
        clean = _validate_contribution(req.form())
    except HTTPError as exc:
        return _render(req, error=exc.message, status=exc.status)
    _insert_contribution(req.user["id"], req.path_params["goal_id"], clean)
    return redirect("/savings")


@router.route("POST", "/savings/<int:goal_id>/delete")
@auth_required
@csrf_protect
def delete_goal_html(req: Request) -> Response:
    _delete_goal(req.user["id"], req.path_params["goal_id"])
    return redirect("/savings")


def _render(req: Request, *, error: str = "", status: int = 200) -> Response:
    goals = _list_goals(req.user["id"])
    csrf = e(req.session["csrf_token"])

    cards = []
    for g in goals:
        saved = int(g["saved_cents"])
        target = int(g["target_amount_cents"])
        pct = min(100, round(100 * saved / target)) if target else 0
        deadline = e(str(g["deadline"])) if g.get("deadline") else "no deadline"
        status_tag = {
            "active": "tag tag-blue",
            "achieved": "tag tag-green",
            "paused": "tag tag-gray",
            "cancelled": "tag tag-gray",
        }.get(g["status"], "tag")
        plan_html = _plan_line_html(g, req.user)
        cards.append(f"""
        <div class="goal-card">
            <div class="goal-header">
                <h3>{e(g["name"])} <span class="{status_tag}">{e(g["status"])}</span></h3>
                <p class="muted">{deadline}</p>
            </div>
            <div class="goal-progress">
                <div class="progress-bar">
                    <div class="progress-fill" style="width: {pct}%"></div>
                </div>
                <p>
                    <strong>{e(format_cents(saved))}</strong>
                    <span class="muted">of {e(format_cents(target))} ({pct}%)</span>
                </p>
                {plan_html}
            </div>
            <form method="post" action="/savings/{g["id"]}/contribute" class="inline-row">
                <input type="hidden" name="csrf_token" value="{csrf}">
                <input type="text" name="amount" placeholder="Add $..." required>
                <input type="date" name="contributed_on" value="{date.today().isoformat()}">
                <button type="submit" class="primary small">Contribute</button>
            </form>
            <form method="post" action="/savings/{g["id"]}/delete" class="inline-form"
                  onsubmit="return confirm('Delete this goal and all contributions?')">
                <input type="hidden" name="csrf_token" value="{csrf}">
                <button type="submit" class="secondary small danger-link">Delete goal</button>
            </form>
        </div>
        """)
    goals_html = "\n".join(cards) or '<p class="muted">No savings goals yet — create one below.</p>'

    return html(render(
        "savings.html",
        title="Savings",
        nav=nav_html("savings"),
        csrf_token=csrf,
        goals_html=goals_html,
        today=date.today().isoformat(),
        error_block=error_block(error),
    ), status=status)


# ---------------------------------------------------------------------------
# JSON API — goals
# ---------------------------------------------------------------------------

@router.route("GET", "/api/v1/savings/goals")
@auth_required
def api_list(req: Request) -> Response:
    items = _list_goals(req.user["id"])
    return json_response({"items": [_serialize_goal(g, user=req.user) for g in items]})


@router.route("POST", "/api/v1/savings/goals")
@auth_required
@csrf_protect
def api_create(req: Request) -> Response:
    clean = _validate_goal(req.json_body() or {})
    new_id = _insert_goal(req.user["id"], clean)
    return json_response(
        _serialize_goal(_get_goal(req.user["id"], new_id), user=req.user),
        status=201,
    )


@router.route("GET", "/api/v1/savings/goals/<int:goal_id>")
@auth_required
def api_get(req: Request) -> Response:
    row = _get_goal(req.user["id"], req.path_params["goal_id"])
    contribs = _list_contributions(row["id"])
    return json_response(_serialize_goal(row, contribs, user=req.user))


@router.route("PUT", "/api/v1/savings/goals/<int:goal_id>")
@auth_required
@csrf_protect
def api_update(req: Request) -> Response:
    clean = _validate_goal(req.json_body() or {}, partial=True)
    if not clean:
        raise HTTPError(400, "No fields to update.")
    _update_goal(req.user["id"], req.path_params["goal_id"], clean)
    return json_response(
        _serialize_goal(_get_goal(req.user["id"], req.path_params["goal_id"]),
                        user=req.user)
    )


@router.route("DELETE", "/api/v1/savings/goals/<int:goal_id>")
@auth_required
@csrf_protect
def api_delete(req: Request) -> Response:
    _delete_goal(req.user["id"], req.path_params["goal_id"])
    return json_response({"ok": True})


@router.route("POST", "/api/v1/savings/goals/<int:goal_id>/contributions")
@auth_required
@csrf_protect
def api_contribute(req: Request) -> Response:
    clean = _validate_contribution(req.json_body() or {})
    _insert_contribution(req.user["id"], req.path_params["goal_id"], clean)
    row = _get_goal(req.user["id"], req.path_params["goal_id"])
    contribs = _list_contributions(row["id"])
    return json_response(_serialize_goal(row, contribs, user=req.user), status=201)


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------

def _insert_goal(user_id: int, clean: dict) -> int:
    with db.transaction():
        cur = db.execute(
            """
            INSERT INTO savings_goals
                (user_id, name, target_amount_cents, deadline, status, notes)
            VALUES (?, ?, ?, ?, 'active', ?)
            """,
            (user_id, clean["name"], clean["target_amount_cents"],
             clean.get("deadline"), clean.get("notes")),
        )
        log_event(user_id, "savings.goal_created", {
            "id": cur.lastrowid, "name": clean["name"],
            "target_cents": clean["target_amount_cents"],
        })
    return cur.lastrowid


def _update_goal(user_id: int, goal_id: int, clean: dict) -> None:
    _get_goal(user_id, goal_id)
    set_clause = ", ".join(f"{k} = ?" for k in clean)
    params = list(clean.values()) + [user_id, goal_id]
    with db.transaction():
        db.execute(
            f"UPDATE savings_goals SET {set_clause}, "
            f"updated_at = CURRENT_TIMESTAMP WHERE user_id = ? AND id = ?",
            params,
        )
        log_event(user_id, "savings.goal_updated",
                  {"id": goal_id, "fields": list(clean.keys())})


def _delete_goal(user_id: int, goal_id: int) -> None:
    row = _get_goal(user_id, goal_id)
    with db.transaction():
        db.execute(
            "DELETE FROM savings_goals WHERE user_id = ? AND id = ?",
            (user_id, goal_id),
        )
        log_event(user_id, "savings.goal_deleted",
                  {"id": goal_id, "name": row["name"]})


def _insert_contribution(user_id: int, goal_id: int, clean: dict) -> int:
    _get_goal(user_id, goal_id)  # auth + existence check
    with db.transaction():
        cur = db.execute(
            """
            INSERT INTO savings_contributions
                (goal_id, amount_cents, contributed_on, note)
            VALUES (?, ?, ?, ?)
            """,
            (goal_id, clean["amount_cents"], clean["contributed_on"], clean.get("note")),
        )
        log_event(user_id, "savings.contribution_added", {
            "goal_id": goal_id,
            "amount_cents": clean["amount_cents"],
        })
    _maybe_mark_achieved(user_id, goal_id)
    return cur.lastrowid
