"""
Expenses routes — discretionary spending tracker.

HTML at /expenses, JSON API at /api/v1/expenses.
Expenses are simpler than bills: just amount + category + date. No status
or recurrence.
"""
from __future__ import annotations

from datetime import date

from app import db
from app.http_utils import HTTPError, Request, Response, html, json_response, redirect
from app.middleware import auth_required, csrf_protect
from app.router import router
from app.services.money import format_cents, parse_dollars
from app.services.progress import log_event
from app.templating import e, error_block, nav_html, render


def _list_expenses(user_id: int, *, limit: int = 100,
                   category: str = "", since: str = "") -> list[dict]:
    sql = (
        "SELECT id, amount_cents, category, spent_on, description, created_at "
        "FROM expenses WHERE user_id = ?"
    )
    params: list = [user_id]
    if category:
        sql += " AND category = ?"
        params.append(category)
    if since:
        sql += " AND spent_on >= ?"
        params.append(since)
    sql += " ORDER BY spent_on DESC, id DESC LIMIT ?"
    params.append(limit)
    return db.query(sql, params)


def _validate(fields: dict, *, partial: bool = False) -> dict:
    out: dict = {}

    def need(key: str) -> bool:
        return key in fields or not partial

    if need("amount_cents") or "amount" in fields:
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
        if cents < 0:
            raise HTTPError(400, "Amount must be non-negative.")
        out["amount_cents"] = cents

    if need("category"):
        cat = (fields.get("category") or "").strip()
        if not cat:
            raise HTTPError(400, "Category is required.")
        if len(cat) > 60:
            raise HTTPError(400, "Category too long (max 60 chars).")
        out["category"] = cat

    if need("spent_on"):
        raw = (fields.get("spent_on") or date.today().isoformat()).strip()
        try:
            date.fromisoformat(raw)
        except ValueError:
            raise HTTPError(400, "Invalid date.")
        out["spent_on"] = raw

    if "description" in fields or not partial:
        desc = (fields.get("description") or "").strip() or None
        out["description"] = desc

    return out


def _serialize(row: dict) -> dict:
    return {
        "id": row["id"],
        "amount_cents": row["amount_cents"],
        "amount_formatted": format_cents(row["amount_cents"]),
        "category": row["category"],
        "spent_on": str(row["spent_on"]),
        "description": row.get("description"),
        "created_at": str(row.get("created_at") or ""),
    }


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

@router.route("GET", "/expenses")
@auth_required
def page(req: Request) -> Response:
    return _render(req)


@router.route("POST", "/expenses")
@auth_required
@csrf_protect
def create_html(req: Request) -> Response:
    try:
        clean = _validate(req.form())
    except HTTPError as exc:
        return _render(req, error=exc.message, status=exc.status)
    _insert(req.user["id"], clean)
    return redirect("/expenses")


@router.route("POST", "/expenses/<int:expense_id>/delete")
@auth_required
@csrf_protect
def delete_html(req: Request) -> Response:
    _delete(req.user["id"], req.path_params["expense_id"])
    return redirect("/expenses")


def _render(req: Request, *, error: str = "", status: int = 200) -> Response:
    expenses = _list_expenses(req.user["id"], limit=200)
    csrf = e(req.session["csrf_token"])

    # Aggregate by category for the summary panel.
    by_cat: dict[str, int] = {}
    total_cents = 0
    for ex in expenses:
        by_cat[ex["category"]] = by_cat.get(ex["category"], 0) + ex["amount_cents"]
        total_cents += ex["amount_cents"]

    summary_rows = "".join(
        f'<li><span class="muted">{e(cat)}</span> '
        f'<strong>{e(format_cents(amt))}</strong></li>'
        for cat, amt in sorted(by_cat.items(), key=lambda kv: -kv[1])
    ) or '<li class="muted">No spending logged yet.</li>'

    expense_rows = "".join(
        f'<tr>'
        f'<td>{e(str(ex["spent_on"]))}</td>'
        f'<td>{e(ex["category"])}</td>'
        f'<td>{e(ex.get("description") or "")}</td>'
        f'<td class="amount">{e(format_cents(ex["amount_cents"]))}</td>'
        f'<td class="actions">'
        f'<form method="post" action="/expenses/{ex["id"]}/delete" '
        f'class="inline-form" onsubmit="return confirm(\'Delete this expense?\')">'
        f'<input type="hidden" name="csrf_token" value="{csrf}">'
        f'<button type="submit" class="secondary small">×</button>'
        f'</form>'
        f'</td></tr>'
        for ex in expenses
    ) or '<tr><td colspan="5" class="muted">No expenses yet.</td></tr>'

    return html(render(
        "expenses.html",
        title="Spending",
        nav=nav_html("expenses"),
        csrf_token=csrf,
        total_amount=e(format_cents(total_cents)),
        summary_rows=summary_rows,
        rows=expense_rows,
        today=date.today().isoformat(),
        error_block=error_block(error),
    ), status=status)


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------

@router.route("GET", "/api/v1/expenses")
@auth_required
def api_list(req: Request) -> Response:
    try:
        limit = max(1, min(500, int(req.get_one("limit", "100"))))
    except ValueError:
        limit = 100
    category = req.get_one("category", "")
    since = req.get_one("since", "")
    items = _list_expenses(req.user["id"], limit=limit, category=category, since=since)
    return json_response({"items": [_serialize(x) for x in items]})


@router.route("POST", "/api/v1/expenses")
@auth_required
@csrf_protect
def api_create(req: Request) -> Response:
    clean = _validate(req.json_body() or {})
    new_id = _insert(req.user["id"], clean)
    row = db.query_one(
        "SELECT * FROM expenses WHERE id = ? AND user_id = ?",
        (new_id, req.user["id"]),
    )
    return json_response(_serialize(row), status=201)


@router.route("DELETE", "/api/v1/expenses/<int:expense_id>")
@auth_required
@csrf_protect
def api_delete(req: Request) -> Response:
    _delete(req.user["id"], req.path_params["expense_id"])
    return json_response({"ok": True})


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------

def _insert(user_id: int, clean: dict) -> int:
    with db.transaction():
        cur = db.execute(
            """
            INSERT INTO expenses (user_id, amount_cents, category, spent_on, description)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, clean["amount_cents"], clean["category"],
             clean["spent_on"], clean.get("description")),
        )
        log_event(user_id, "expense.logged", {
            "id": cur.lastrowid,
            "amount_cents": clean["amount_cents"],
            "category": clean["category"],
        })
    return cur.lastrowid


def _delete(user_id: int, expense_id: int) -> None:
    row = db.query_one(
        "SELECT id, amount_cents, category FROM expenses WHERE id = ? AND user_id = ?",
        (expense_id, user_id),
    )
    if row is None:
        raise HTTPError(404, "Expense not found")
    with db.transaction():
        db.execute("DELETE FROM expenses WHERE id = ? AND user_id = ?",
                   (expense_id, user_id))
        log_event(user_id, "expense.deleted",
                  {"id": expense_id, "amount_cents": row["amount_cents"]})
