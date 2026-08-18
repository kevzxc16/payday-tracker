"""
Bills routes — recurring or one-time obligations with a due date.

HTML at /bills, JSON API at /api/v1/bills/*.

Lifecycle:
  unpaid → paid     (user marked it paid)
  unpaid → overdue  (auto-set when past due — handled by scheduler in Phase 3)
  paid (recurring) → new unpaid row for next period (auto-generated)
"""
from __future__ import annotations

from datetime import date

from app import db
from app.http_utils import HTTPError, Request, Response, html, json_response, redirect
from app.middleware import auth_required, csrf_protect
from app.router import router
from app.services.money import format_cents, parse_dollars
from app.services.progress import log_event
from app.templating import e, error_block, info_block, nav_html, render, render_rows

VALID_STATUSES = {"unpaid", "paid", "overdue", "skipped"}
VALID_RECURRENCES = {"weekly", "biweekly", "monthly", "yearly"}


# ---------------------------------------------------------------------------
# Domain helpers
# ---------------------------------------------------------------------------

def _list_bills(user_id: int, status_filter: str = "") -> list[dict]:
    """Fetch all bills for a user, optionally filtered by status."""
    sql = (
        "SELECT id, name, amount_cents, due_date, is_recurring, recurrence, "
        "status, paid_on, category, notes "
        "FROM bills WHERE user_id = ?"
    )
    params: list = [user_id]
    if status_filter:
        sql += " AND status = ?"
        params.append(status_filter)
    sql += " ORDER BY due_date ASC, id ASC"
    return db.query(sql, params)


def _get_bill(user_id: int, bill_id: int) -> dict:
    """Fetch a single bill scoped to the user. Raises 404 if not found."""
    row = db.query_one(
        "SELECT * FROM bills WHERE id = ? AND user_id = ?",
        (bill_id, user_id),
    )
    if row is None:
        raise HTTPError(404, "Bill not found")
    return row


def _validate_bill(fields: dict, *, partial: bool = False) -> dict:
    """Validate + normalize bill fields. `partial` means only check present keys."""
    out: dict = {}

    def need(key: str) -> bool:
        return key in fields or not partial

    if need("name"):
        name = (fields.get("name") or "").strip()
        if not name:
            raise HTTPError(400, "Bill name is required.")
        if len(name) > 120:
            raise HTTPError(400, "Bill name is too long (max 120 chars).")
        out["name"] = name

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

    if need("due_date"):
        raw = (fields.get("due_date") or "").strip()
        try:
            date.fromisoformat(raw)
        except ValueError:
            raise HTTPError(400, "Invalid due date.")
        out["due_date"] = raw

    if "is_recurring" in fields or not partial:
        raw = fields.get("is_recurring")
        is_recurring = 1 if str(raw).lower() in ("1", "true", "on", "yes") else 0
        out["is_recurring"] = is_recurring

    if "recurrence" in fields or not partial:
        rec = (fields.get("recurrence") or "").strip().lower() or None
        if rec and rec not in VALID_RECURRENCES:
            raise HTTPError(400, "Invalid recurrence interval.")
        # If user said is_recurring but didn't pick interval, default monthly.
        if out.get("is_recurring") == 1 and not rec:
            rec = "monthly"
        if out.get("is_recurring") == 0:
            rec = None
        out["recurrence"] = rec

    if "category" in fields or not partial:
        cat = (fields.get("category") or "").strip() or None
        if cat and len(cat) > 60:
            raise HTTPError(400, "Category name too long.")
        out["category"] = cat

    if "notes" in fields or not partial:
        notes = (fields.get("notes") or "").strip() or None
        out["notes"] = notes

    if "status" in fields:
        st = (fields.get("status") or "").strip().lower()
        if st not in VALID_STATUSES:
            raise HTTPError(400, "Invalid status.")
        out["status"] = st

    return out


def _serialize_bill(row: dict) -> dict:
    """Shape a DB row for the JSON API."""
    return {
        "id": row["id"],
        "name": row["name"],
        "amount_cents": row["amount_cents"],
        "amount_formatted": format_cents(row["amount_cents"]),
        "due_date": str(row["due_date"]) if row.get("due_date") else None,
        "is_recurring": bool(row.get("is_recurring")),
        "recurrence": row.get("recurrence"),
        "status": row["status"],
        "paid_on": str(row["paid_on"]) if row.get("paid_on") else None,
        "category": row.get("category"),
        "notes": row.get("notes"),
    }


def _next_due_date(d: date, recurrence: str) -> date:
    """Step a due date forward by one recurrence cycle."""
    if recurrence == "weekly":
        from datetime import timedelta
        return d + timedelta(days=7)
    if recurrence == "biweekly":
        from datetime import timedelta
        return d + timedelta(days=14)
    if recurrence == "monthly":
        from app.services.payday import _add_months
        return _add_months(d, 1)
    if recurrence == "yearly":
        from app.services.payday import _add_months
        return _add_months(d, 12)
    return d


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

@router.route("GET", "/bills")
@auth_required
def bills_page(req: Request) -> Response:
    return _render_bills(req)


@router.route("POST", "/bills")
@auth_required
@csrf_protect
def bills_create(req: Request) -> Response:
    form = req.form()
    try:
        clean = _validate_bill(form, partial=False)
    except HTTPError as exc:
        return _render_bills(req, error=exc.message, status=exc.status)
    _insert_bill(req.user["id"], clean)
    return redirect("/bills")


@router.route("POST", "/bills/<int:bill_id>/pay")
@auth_required
@csrf_protect
def bills_mark_paid(req: Request) -> Response:
    _mark_paid(req.user["id"], req.path_params["bill_id"])
    return redirect("/bills")


@router.route("POST", "/bills/<int:bill_id>/delete")
@auth_required
@csrf_protect
def bills_delete_html(req: Request) -> Response:
    _delete_bill(req.user["id"], req.path_params["bill_id"])
    return redirect("/bills")


def _bill_row_html(b: dict, csrf: str) -> str:
    """One <tr> for the bills table. Pulled out so we can reuse across groups."""
    status_class = {
        "unpaid": "tag tag-yellow",
        "paid": "tag tag-green",
        "overdue": "tag tag-red",
        "skipped": "tag tag-gray",
    }.get(b["status"], "tag")

    actions = ""
    if b["status"] in ("unpaid", "overdue"):
        actions = (
            f'<form method="post" action="/bills/{b["id"]}/pay" class="inline-form">'
            f'<input type="hidden" name="csrf_token" value="{csrf}">'
            f'<button type="submit" class="primary small">Mark paid</button>'
            f'</form> '
        )
    actions += (
        f'<form method="post" action="/bills/{b["id"]}/delete" class="inline-form" '
        f'onsubmit="return confirm(\'Delete this bill?\')">'
        f'<input type="hidden" name="csrf_token" value="{csrf}">'
        f'<button type="submit" class="secondary small">Delete</button>'
        f'</form>'
    )

    recurrence_note = ""
    if b["is_recurring"]:
        recurrence_note = f' <span class="muted">· recurs {e(b["recurrence"] or "monthly")}</span>'

    return (
        f'<tr>'
        f'<td>{e(b["name"])}{recurrence_note}</td>'
        f'<td>{e(b["category"] or "")}</td>'
        f'<td>{e(str(b["due_date"]))}</td>'
        f'<td class="amount">{e(format_cents(b["amount_cents"]))}</td>'
        f'<td><span class="{status_class}">{e(b["status"])}</span></td>'
        f'<td class="actions">{actions}</td>'
        f'</tr>'
    )


def _bills_table_html(rows_html: str) -> str:
    """Wrap a list of <tr> rows in the standard bills table with headers."""
    return (
        '<table class="data-table">'
        '<thead><tr>'
        '<th>Name</th><th>Category</th><th>Due</th>'
        '<th class="amount">Amount</th><th>Status</th><th>Actions</th>'
        '</tr></thead>'
        f'<tbody>{rows_html}</tbody>'
        '</table>'
    )


def _group_header_html(label: str, subtitle: str, total_cents: int,
                       total_label: str = "unpaid") -> str:
    """A magazine-style section header: small-caps label + dollar total."""
    return (
        f'<div class="group-divider">'
        f'<span class="group-label">{e(label)}'
        f'<span class="group-sublabel"> · {e(subtitle)}</span></span>'
        f'<span class="group-total amount">'
        f'{e(format_cents(total_cents))} {e(total_label)}'
        f'</span>'
        f'</div>'
    )


def _render_bills(req: Request, *, error: str = "", status: int = 200) -> Response:
    from app.services.payday import current_period

    bills = _list_bills(req.user["id"])
    csrf = e(req.session["csrf_token"])

    # Compute the user's current pay period so we can group unpaid bills
    # against the next-payday boundary. This is the whole point of the
    # "Bill Buffer Planner" framing — bills due before payday vs after.
    today = date.today()
    user = req.user
    anchor = (
        date.fromisoformat(user["first_payday"])
        if isinstance(user["first_payday"], str) else user["first_payday"]
    )
    period = current_period(anchor, user["pay_schedule"], today)
    next_payday = period.next_payday

    # Partition. Paid bills don't get grouped by payday — they're history.
    unpaid_before: list[dict] = []
    unpaid_after: list[dict] = []
    paid: list[dict] = []
    for b in bills:
        if b["status"] == "paid":
            paid.append(b)
            continue
        due = b["due_date"]
        if isinstance(due, str):
            due = date.fromisoformat(due)
        if due < next_payday:
            unpaid_before.append(b)
        else:
            unpaid_after.append(b)

    # Paid bills go most-recent-first by when they were paid (history view).
    paid.sort(key=lambda b: str(b.get("paid_on") or b["due_date"]), reverse=True)

    next_payday_str = next_payday.isoformat()

    # Build the page body section by section. Each unpaid group only renders
    # if it has bills; this keeps the layout honest when one bucket is empty.
    sections: list[str] = []
    if unpaid_before:
        total = sum(b["amount_cents"] for b in unpaid_before)
        sections.append(_group_header_html(
            "Before next payday",
            f"due before {next_payday_str}",
            total,
        ))
        sections.append(_bills_table_html(
            "".join(_bill_row_html(b, csrf) for b in unpaid_before)
        ))

    if unpaid_after:
        total = sum(b["amount_cents"] for b in unpaid_after)
        sections.append(_group_header_html(
            "After next payday",
            f"due on or after {next_payday_str}",
            total,
        ))
        sections.append(_bills_table_html(
            "".join(_bill_row_html(b, csrf) for b in unpaid_after)
        ))

    if paid:
        total = sum(b["amount_cents"] for b in paid)
        sections.append(_group_header_html(
            "Recently paid",
            f"{len(paid)} {'bill' if len(paid) == 1 else 'bills'}",
            total,
            total_label="total",
        ))
        sections.append(_bills_table_html(
            "".join(_bill_row_html(b, csrf) for b in paid)
        ))

    if not sections:
        bills_html = '<p class="muted">No bills yet — add one below.</p>'
    else:
        bills_html = "".join(sections)

    return html(render(
        "bills.html",
        title="Bills",
        nav=nav_html("bills"),
        csrf_token=csrf,
        bills_html=bills_html,
        error_block=error_block(error),
    ), status=status)


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------

@router.route("GET", "/api/v1/bills")
@auth_required
def api_list(req: Request) -> Response:
    status_filter = req.get_one("status", "")
    if status_filter and status_filter not in VALID_STATUSES:
        raise HTTPError(400, "Invalid status filter.")
    items = _list_bills(req.user["id"], status_filter)
    return json_response({"items": [_serialize_bill(b) for b in items]})


@router.route("POST", "/api/v1/bills")
@auth_required
@csrf_protect
def api_create(req: Request) -> Response:
    payload = req.json_body() or {}
    clean = _validate_bill(payload, partial=False)
    bill_id = _insert_bill(req.user["id"], clean)
    row = _get_bill(req.user["id"], bill_id)
    return json_response(_serialize_bill(row), status=201)


@router.route("GET", "/api/v1/bills/<int:bill_id>")
@auth_required
def api_get(req: Request) -> Response:
    row = _get_bill(req.user["id"], req.path_params["bill_id"])
    return json_response(_serialize_bill(row))


@router.route("PUT", "/api/v1/bills/<int:bill_id>")
@auth_required
@csrf_protect
def api_update(req: Request) -> Response:
    payload = req.json_body() or {}
    clean = _validate_bill(payload, partial=True)
    if not clean:
        raise HTTPError(400, "No fields to update.")
    _update_bill(req.user["id"], req.path_params["bill_id"], clean)
    row = _get_bill(req.user["id"], req.path_params["bill_id"])
    return json_response(_serialize_bill(row))


@router.route("DELETE", "/api/v1/bills/<int:bill_id>")
@auth_required
@csrf_protect
def api_delete(req: Request) -> Response:
    _delete_bill(req.user["id"], req.path_params["bill_id"])
    return json_response({"ok": True})


@router.route("POST", "/api/v1/bills/<int:bill_id>/pay")
@auth_required
@csrf_protect
def api_mark_paid(req: Request) -> Response:
    _mark_paid(req.user["id"], req.path_params["bill_id"])
    row = _get_bill(req.user["id"], req.path_params["bill_id"])
    return json_response(_serialize_bill(row))


# ---------------------------------------------------------------------------
# Mutations (shared between HTML and JSON)
# ---------------------------------------------------------------------------

def _insert_bill(user_id: int, clean: dict) -> int:
    with db.transaction():
        cur = db.execute(
            """
            INSERT INTO bills
                (user_id, name, amount_cents, due_date, is_recurring,
                 recurrence, status, category, notes)
            VALUES (?, ?, ?, ?, ?, ?, 'unpaid', ?, ?)
            """,
            (
                user_id,
                clean["name"], clean["amount_cents"], clean["due_date"],
                clean.get("is_recurring", 0), clean.get("recurrence"),
                clean.get("category"), clean.get("notes"),
            ),
        )
        bill_id = cur.lastrowid
        log_event(user_id, "bill.created", {
            "id": bill_id,
            "name": clean["name"],
            "amount_cents": clean["amount_cents"],
        })
    return bill_id


def _update_bill(user_id: int, bill_id: int, clean: dict) -> None:
    _get_bill(user_id, bill_id)  # auth + existence check
    set_clause = ", ".join(f"{k} = ?" for k in clean)
    params = list(clean.values()) + [user_id, bill_id]
    with db.transaction():
        db.execute(
            f"UPDATE bills SET {set_clause}, updated_at = CURRENT_TIMESTAMP "
            f"WHERE user_id = ? AND id = ?",
            params,
        )
        log_event(user_id, "bill.updated", {"id": bill_id, "fields": list(clean.keys())})


def _delete_bill(user_id: int, bill_id: int) -> None:
    row = _get_bill(user_id, bill_id)
    with db.transaction():
        db.execute("DELETE FROM bills WHERE user_id = ? AND id = ?", (user_id, bill_id))
        log_event(user_id, "bill.deleted", {"id": bill_id, "name": row["name"]})


def _mark_paid(user_id: int, bill_id: int) -> None:
    """
    Mark a bill paid. If recurring, also create the next occurrence as unpaid.
    """
    row = _get_bill(user_id, bill_id)
    if row["status"] == "paid":
        return  # idempotent

    today_iso = date.today().isoformat()
    with db.transaction():
        db.execute(
            """
            UPDATE bills SET status = 'paid', paid_on = ?,
                             updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ? AND id = ?
            """,
            (today_iso, user_id, bill_id),
        )
        log_event(user_id, "bill.paid", {"id": bill_id, "name": row["name"],
                                          "amount_cents": row["amount_cents"]})

        # If recurring, spawn the next occurrence so the user sees it.
        if row.get("is_recurring") and row.get("recurrence"):
            current_due = row["due_date"]
            if isinstance(current_due, str):
                current_due = date.fromisoformat(current_due)
            next_due = _next_due_date(current_due, row["recurrence"])
            # Don't spawn a duplicate if the next one is already there.
            exists = db.query_one(
                """SELECT id FROM bills WHERE user_id = ? AND name = ?
                                AND due_date = ? AND amount_cents = ?""",
                (user_id, row["name"], next_due.isoformat(), row["amount_cents"]),
            )
            if exists is None:
                cur = db.execute(
                    """
                    INSERT INTO bills
                        (user_id, name, amount_cents, due_date, is_recurring,
                         recurrence, status, category, notes)
                    VALUES (?, ?, ?, ?, 1, ?, 'unpaid', ?, ?)
                    """,
                    (
                        user_id, row["name"], row["amount_cents"],
                        next_due.isoformat(), row["recurrence"],
                        row.get("category"), row.get("notes"),
                    ),
                )
                log_event(user_id, "bill.next_occurrence_created",
                          {"parent_id": bill_id, "next_id": cur.lastrowid,
                           "due_date": next_due.isoformat()})
