"""
Debts routes — debts with running balance + payments.

Each payment auto-decrements the debt's current_balance_cents. When the
balance hits zero, status flips to 'paid_off'.

HTML at /debts, JSON API at /api/v1/debts/*.
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

VALID_STATUSES = {"active", "paid_off", "closed"}


def _list_debts(user_id: int) -> list[dict]:
    return db.query(
        """
        SELECT id, name, starting_balance_cents, current_balance_cents,
               minimum_payment_cents, target_payoff_date, interest_rate_bps,
               status, notes
        FROM debts WHERE user_id = ?
        ORDER BY status = 'active' DESC, current_balance_cents DESC
        """,
        (user_id,),
    )


def _get_debt(user_id: int, debt_id: int) -> dict:
    row = db.query_one(
        "SELECT * FROM debts WHERE id = ? AND user_id = ?",
        (debt_id, user_id),
    )
    if row is None:
        raise HTTPError(404, "Debt not found")
    return row


def _list_payments(debt_id: int) -> list[dict]:
    return db.query(
        """
        SELECT id, amount_cents, paid_on, note, created_at
        FROM debt_payments WHERE debt_id = ?
        ORDER BY paid_on DESC, id DESC
        """,
        (debt_id,),
    )


def _validate_debt(fields: dict, *, partial: bool = False) -> dict:
    out: dict = {}

    def need(k):
        return k in fields or not partial

    if need("name"):
        n = (fields.get("name") or "").strip()
        if not n:
            raise HTTPError(400, "Debt name is required.")
        out["name"] = n[:120]

    if need("starting_balance_cents") or "starting_balance" in fields:
        cents = _money(fields, "starting_balance_cents", "starting_balance")
        if cents < 0:
            raise HTTPError(400, "Starting balance must be non-negative.")
        out["starting_balance_cents"] = cents
        # If creating new, current balance starts equal to starting balance
        # unless explicitly overridden.
        if not partial and "current_balance_cents" not in fields \
                and "current_balance" not in fields:
            out["current_balance_cents"] = cents

    if "current_balance_cents" in fields or "current_balance" in fields:
        cents = _money(fields, "current_balance_cents", "current_balance")
        if cents < 0:
            raise HTTPError(400, "Current balance must be non-negative.")
        out["current_balance_cents"] = cents

    if need("minimum_payment_cents") or "minimum_payment" in fields:
        cents = _money(fields, "minimum_payment_cents", "minimum_payment", default=0)
        if cents < 0:
            raise HTTPError(400, "Minimum payment must be non-negative.")
        out["minimum_payment_cents"] = cents

    if "target_payoff_date" in fields or not partial:
        raw = (fields.get("target_payoff_date") or "").strip() or None
        if raw is not None:
            try:
                date.fromisoformat(raw)
            except ValueError:
                raise HTTPError(400, "Invalid target payoff date.")
        out["target_payoff_date"] = raw

    if "interest_rate_bps" in fields or "interest_rate_pct" in fields or not partial:
        if "interest_rate_bps" in fields:
            try:
                bps = int(fields["interest_rate_bps"])
            except (TypeError, ValueError):
                raise HTTPError(400, "Invalid interest rate.")
        elif "interest_rate_pct" in fields:
            try:
                pct = float(fields.get("interest_rate_pct") or 0)
            except (TypeError, ValueError):
                raise HTTPError(400, "Invalid interest rate.")
            bps = int(round(pct * 100))
        else:
            bps = None
        if bps is not None and (bps < 0 or bps > 100_000):
            raise HTTPError(400, "Interest rate out of range.")
        out["interest_rate_bps"] = bps

    if "status" in fields:
        st = (fields.get("status") or "").strip().lower()
        if st not in VALID_STATUSES:
            raise HTTPError(400, "Invalid status.")
        out["status"] = st

    if "notes" in fields or not partial:
        out["notes"] = (fields.get("notes") or "").strip() or None

    return out


def _money(fields: dict, cents_key: str, dollars_key: str, default=None) -> int:
    """Pull money value from either cents or dollar form."""
    if cents_key in fields:
        try:
            return int(fields[cents_key])
        except (TypeError, ValueError):
            raise HTTPError(400, f"Invalid {cents_key}.")
    if dollars_key in fields:
        try:
            return parse_dollars(fields[dollars_key])
        except ValueError as exc:
            raise HTTPError(400, str(exc))
    if default is not None:
        return default
    raise HTTPError(400, f"{dollars_key} is required.")


def _validate_payment(fields: dict) -> dict:
    out: dict = {}
    out["amount_cents"] = _money(fields, "amount_cents", "amount")
    if out["amount_cents"] <= 0:
        raise HTTPError(400, "Payment must be positive.")
    raw = (fields.get("paid_on") or date.today().isoformat()).strip()
    try:
        date.fromisoformat(raw)
    except ValueError:
        raise HTTPError(400, "Invalid payment date.")
    out["paid_on"] = raw
    out["note"] = (fields.get("note") or "").strip() or None
    return out


def _serialize_debt(row: dict, payments: list[dict] | None = None) -> dict:
    start = int(row["starting_balance_cents"])
    cur = int(row["current_balance_cents"])
    paid = start - cur
    pct = round(100 * paid / start, 1) if start else 0.0
    data = {
        "id": row["id"],
        "name": row["name"],
        "starting_balance_cents": start,
        "starting_balance_formatted": format_cents(start),
        "current_balance_cents": cur,
        "current_balance_formatted": format_cents(cur),
        "paid_so_far_cents": paid,
        "paid_so_far_formatted": format_cents(paid),
        "progress_pct": pct,
        "minimum_payment_cents": row["minimum_payment_cents"],
        "minimum_payment_formatted": format_cents(row["minimum_payment_cents"]),
        "target_payoff_date": str(row["target_payoff_date"]) if row.get("target_payoff_date") else None,
        "interest_rate_bps": row.get("interest_rate_bps"),
        "interest_rate_pct": (row["interest_rate_bps"] / 100) if row.get("interest_rate_bps") else None,
        "status": row["status"],
        "notes": row.get("notes"),
    }
    if payments is not None:
        data["payments"] = [
            {
                "id": p["id"],
                "amount_cents": p["amount_cents"],
                "amount_formatted": format_cents(p["amount_cents"]),
                "paid_on": str(p["paid_on"]),
                "note": p.get("note"),
            } for p in payments
        ]
    return data


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

@router.route("GET", "/debts")
@auth_required
def page(req: Request) -> Response:
    return _render(req)


@router.route("POST", "/debts")
@auth_required
@csrf_protect
def create_html(req: Request) -> Response:
    try:
        clean = _validate_debt(req.form())
    except HTTPError as exc:
        return _render(req, error=exc.message, status=exc.status)
    _insert_debt(req.user["id"], clean)
    return redirect("/debts")


@router.route("POST", "/debts/<int:debt_id>/pay")
@auth_required
@csrf_protect
def pay_html(req: Request) -> Response:
    try:
        clean = _validate_payment(req.form())
    except HTTPError as exc:
        return _render(req, error=exc.message, status=exc.status)
    _record_payment(req.user["id"], req.path_params["debt_id"], clean)
    return redirect("/debts")


@router.route("POST", "/debts/<int:debt_id>/delete")
@auth_required
@csrf_protect
def delete_html(req: Request) -> Response:
    _delete_debt(req.user["id"], req.path_params["debt_id"])
    return redirect("/debts")


def _render(req: Request, *, error: str = "", status: int = 200) -> Response:
    debts = _list_debts(req.user["id"])
    csrf = e(req.session["csrf_token"])

    cards = []
    for d in debts:
        start = int(d["starting_balance_cents"])
        cur = int(d["current_balance_cents"])
        paid = start - cur
        pct = min(100, round(100 * paid / start)) if start else 0
        status_tag = {
            "active": "tag tag-red",
            "paid_off": "tag tag-green",
            "closed": "tag tag-gray",
        }.get(d["status"], "tag")
        rate_text = ""
        if d.get("interest_rate_bps"):
            rate_text = f' · {d["interest_rate_bps"]/100:.2f}% APR'
        target_text = ""
        if d.get("target_payoff_date"):
            target_text = f' · target {e(str(d["target_payoff_date"]))}'

        payment_form = ""
        if d["status"] == "active":
            payment_form = f"""
            <form method="post" action="/debts/{d["id"]}/pay" class="inline-row">
                <input type="hidden" name="csrf_token" value="{csrf}">
                <input type="text" name="amount" placeholder="Payment $..." required>
                <input type="date" name="paid_on" value="{date.today().isoformat()}">
                <button type="submit" class="primary small">Record payment</button>
            </form>"""

        cards.append(f"""
        <div class="debt-card">
            <div class="debt-header">
                <h3>{e(d["name"])} <span class="{status_tag}">{e(d["status"])}</span></h3>
                <p class="muted">Min payment: {e(format_cents(d["minimum_payment_cents"]))}{rate_text}{target_text}</p>
            </div>
            <div class="goal-progress">
                <div class="progress-bar">
                    <div class="progress-fill green" style="width: {pct}%"></div>
                </div>
                <p>
                    <strong>{e(format_cents(cur))}</strong>
                    <span class="muted">remaining of {e(format_cents(start))} ({pct}% paid)</span>
                </p>
            </div>
            {payment_form}
            <form method="post" action="/debts/{d["id"]}/delete" class="inline-form"
                  onsubmit="return confirm('Delete this debt and its payment history?')">
                <input type="hidden" name="csrf_token" value="{csrf}">
                <button type="submit" class="secondary small danger-link">Delete debt</button>
            </form>
        </div>""")
    debts_html = "\n".join(cards) or '<p class="muted">No debts tracked yet — add one below.</p>'

    return html(render(
        "debts.html",
        title="Debts",
        nav=nav_html("debts"),
        csrf_token=csrf,
        debts_html=debts_html,
        error_block=error_block(error),
    ), status=status)


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------

@router.route("GET", "/api/v1/debts")
@auth_required
def api_list(req: Request) -> Response:
    items = _list_debts(req.user["id"])
    return json_response({"items": [_serialize_debt(d) for d in items]})


@router.route("POST", "/api/v1/debts")
@auth_required
@csrf_protect
def api_create(req: Request) -> Response:
    clean = _validate_debt(req.json_body() or {})
    new_id = _insert_debt(req.user["id"], clean)
    return json_response(_serialize_debt(_get_debt(req.user["id"], new_id)), status=201)


@router.route("GET", "/api/v1/debts/<int:debt_id>")
@auth_required
def api_get(req: Request) -> Response:
    row = _get_debt(req.user["id"], req.path_params["debt_id"])
    payments = _list_payments(row["id"])
    return json_response(_serialize_debt(row, payments))


@router.route("PUT", "/api/v1/debts/<int:debt_id>")
@auth_required
@csrf_protect
def api_update(req: Request) -> Response:
    clean = _validate_debt(req.json_body() or {}, partial=True)
    if not clean:
        raise HTTPError(400, "No fields to update.")
    _update_debt(req.user["id"], req.path_params["debt_id"], clean)
    return json_response(_serialize_debt(_get_debt(req.user["id"], req.path_params["debt_id"])))


@router.route("DELETE", "/api/v1/debts/<int:debt_id>")
@auth_required
@csrf_protect
def api_delete(req: Request) -> Response:
    _delete_debt(req.user["id"], req.path_params["debt_id"])
    return json_response({"ok": True})


@router.route("POST", "/api/v1/debts/<int:debt_id>/payments")
@auth_required
@csrf_protect
def api_pay(req: Request) -> Response:
    clean = _validate_payment(req.json_body() or {})
    _record_payment(req.user["id"], req.path_params["debt_id"], clean)
    row = _get_debt(req.user["id"], req.path_params["debt_id"])
    payments = _list_payments(row["id"])
    return json_response(_serialize_debt(row, payments), status=201)


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------

def _insert_debt(user_id: int, clean: dict) -> int:
    with db.transaction():
        cur = db.execute(
            """
            INSERT INTO debts
                (user_id, name, starting_balance_cents, current_balance_cents,
                 minimum_payment_cents, target_payoff_date, interest_rate_bps,
                 status, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)
            """,
            (
                user_id, clean["name"],
                clean["starting_balance_cents"],
                clean.get("current_balance_cents", clean["starting_balance_cents"]),
                clean["minimum_payment_cents"],
                clean.get("target_payoff_date"),
                clean.get("interest_rate_bps"),
                clean.get("notes"),
            ),
        )
        log_event(user_id, "debt.created", {
            "id": cur.lastrowid, "name": clean["name"],
            "starting_cents": clean["starting_balance_cents"],
        })
    return cur.lastrowid


def _update_debt(user_id: int, debt_id: int, clean: dict) -> None:
    _get_debt(user_id, debt_id)
    set_clause = ", ".join(f"{k} = ?" for k in clean)
    params = list(clean.values()) + [user_id, debt_id]
    with db.transaction():
        db.execute(
            f"UPDATE debts SET {set_clause}, updated_at = CURRENT_TIMESTAMP "
            f"WHERE user_id = ? AND id = ?",
            params,
        )
        log_event(user_id, "debt.updated",
                  {"id": debt_id, "fields": list(clean.keys())})


def _delete_debt(user_id: int, debt_id: int) -> None:
    row = _get_debt(user_id, debt_id)
    with db.transaction():
        db.execute("DELETE FROM debts WHERE user_id = ? AND id = ?",
                   (user_id, debt_id))
        log_event(user_id, "debt.deleted",
                  {"id": debt_id, "name": row["name"]})


def _record_payment(user_id: int, debt_id: int, clean: dict) -> int:
    """Record a payment, decrement balance, auto-paid-off if zero."""
    row = _get_debt(user_id, debt_id)
    if row["status"] != "active":
        raise HTTPError(400, "Can't record a payment on a non-active debt.")
    amount = clean["amount_cents"]
    new_balance = max(0, int(row["current_balance_cents"]) - amount)
    new_status = "paid_off" if new_balance == 0 else "active"

    with db.transaction():
        cur = db.execute(
            """
            INSERT INTO debt_payments (debt_id, amount_cents, paid_on, note)
            VALUES (?, ?, ?, ?)
            """,
            (debt_id, amount, clean["paid_on"], clean.get("note")),
        )
        db.execute(
            """
            UPDATE debts SET current_balance_cents = ?, status = ?,
                             updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ? AND id = ?
            """,
            (new_balance, new_status, user_id, debt_id),
        )
        log_event(user_id, "debt.payment_recorded", {
            "debt_id": debt_id, "payment_id": cur.lastrowid,
            "amount_cents": amount, "new_balance_cents": new_balance,
        })
        if new_status == "paid_off":
            log_event(user_id, "debt.paid_off",
                      {"id": debt_id, "name": row["name"]})
    return cur.lastrowid
