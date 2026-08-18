"""
Income routes — recorded paychecks.

HTML at /income, JSON API at /api/v1/paychecks.
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


def _list_paychecks(user_id: int, limit: int = 100) -> list[dict]:
    return db.query(
        """
        SELECT id, amount_cents, received_on, note, created_at
        FROM paychecks WHERE user_id = ?
        ORDER BY received_on DESC, id DESC LIMIT ?
        """,
        (user_id, limit),
    )


def _validate(fields: dict) -> dict:
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
    if cents < 0:
        raise HTTPError(400, "Amount must be non-negative.")
    out["amount_cents"] = cents

    raw = (fields.get("received_on") or date.today().isoformat()).strip()
    try:
        date.fromisoformat(raw)
    except ValueError:
        raise HTTPError(400, "Invalid date.")
    out["received_on"] = raw

    out["note"] = (fields.get("note") or "").strip() or None
    return out


def _serialize(row: dict) -> dict:
    return {
        "id": row["id"],
        "amount_cents": row["amount_cents"],
        "amount_formatted": format_cents(row["amount_cents"]),
        "received_on": str(row["received_on"]),
        "note": row.get("note"),
    }


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

@router.route("GET", "/income")
@auth_required
def page(req: Request) -> Response:
    return _render(req)


@router.route("POST", "/income")
@auth_required
@csrf_protect
def create_html(req: Request) -> Response:
    try:
        clean = _validate(req.form())
    except HTTPError as exc:
        return _render(req, error=exc.message, status=exc.status)
    _insert(req.user["id"], clean)
    return redirect("/income")


@router.route("POST", "/income/<int:paycheck_id>/delete")
@auth_required
@csrf_protect
def delete_html(req: Request) -> Response:
    _delete(req.user["id"], req.path_params["paycheck_id"])
    return redirect("/income")


def _render(req: Request, *, error: str = "", status: int = 200) -> Response:
    items = _list_paychecks(req.user["id"], limit=200)
    csrf = e(req.session["csrf_token"])
    total = sum(p["amount_cents"] for p in items)

    rows = "".join(
        f'<tr>'
        f'<td>{e(str(p["received_on"]))}</td>'
        f'<td class="amount"><strong>{e(format_cents(p["amount_cents"]))}</strong></td>'
        f'<td>{e(p.get("note") or "")}</td>'
        f'<td class="actions">'
        f'<form method="post" action="/income/{p["id"]}/delete" '
        f'class="inline-form" onsubmit="return confirm(\'Delete this paycheck?\')">'
        f'<input type="hidden" name="csrf_token" value="{csrf}">'
        f'<button type="submit" class="secondary small">×</button>'
        f'</form></td></tr>'
        for p in items
    ) or '<tr><td colspan="4" class="muted">No paychecks recorded yet.</td></tr>'

    return html(render(
        "income.html",
        title="Income",
        nav=nav_html("income"),
        csrf_token=csrf,
        total_amount=e(format_cents(total)),
        rows=rows,
        today=date.today().isoformat(),
        error_block=error_block(error),
    ), status=status)


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------

@router.route("GET", "/api/v1/paychecks")
@auth_required
def api_list(req: Request) -> Response:
    try:
        limit = max(1, min(500, int(req.get_one("limit", "100"))))
    except ValueError:
        limit = 100
    return json_response({"items": [_serialize(p) for p in _list_paychecks(req.user["id"], limit)]})


@router.route("POST", "/api/v1/paychecks")
@auth_required
@csrf_protect
def api_create(req: Request) -> Response:
    clean = _validate(req.json_body() or {})
    new_id = _insert(req.user["id"], clean)
    row = db.query_one("SELECT * FROM paychecks WHERE id = ? AND user_id = ?",
                       (new_id, req.user["id"]))
    return json_response(_serialize(row), status=201)


@router.route("DELETE", "/api/v1/paychecks/<int:paycheck_id>")
@auth_required
@csrf_protect
def api_delete(req: Request) -> Response:
    _delete(req.user["id"], req.path_params["paycheck_id"])
    return json_response({"ok": True})


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------

def _insert(user_id: int, clean: dict) -> int:
    with db.transaction():
        cur = db.execute(
            """
            INSERT INTO paychecks (user_id, amount_cents, received_on, note)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, clean["amount_cents"], clean["received_on"], clean.get("note")),
        )
        log_event(user_id, "income.recorded",
                  {"id": cur.lastrowid, "amount_cents": clean["amount_cents"]})
    return cur.lastrowid


def _delete(user_id: int, paycheck_id: int) -> None:
    row = db.query_one(
        "SELECT id, amount_cents FROM paychecks WHERE id = ? AND user_id = ?",
        (paycheck_id, user_id),
    )
    if row is None:
        raise HTTPError(404, "Paycheck not found")
    with db.transaction():
        db.execute("DELETE FROM paychecks WHERE id = ? AND user_id = ?",
                   (paycheck_id, user_id))
        log_event(user_id, "income.deleted",
                  {"id": paycheck_id, "amount_cents": row["amount_cents"]})
