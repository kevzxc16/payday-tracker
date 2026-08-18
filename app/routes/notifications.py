"""
Notifications and activity panel routes.

- /notifications      — HTML list of the user's notifications
- /api/v1/notifications, /api/v1/notifications/<id>/dismiss — JSON API
- /activity           — HTML feed of recent activity log events
- /api/v1/activity    — JSON
"""
from __future__ import annotations

from app import notifications as notif_svc
from app.http_utils import HTTPError, Request, Response, html, json_response, redirect
from app.middleware import auth_required, csrf_protect
from app.router import router
from app.services.progress import recent_events
from app.templating import e, error_block, nav_html, render

_EVENT_FRIENDLY = {
    "user.signup": "Created account",
    "user.login": "Logged in",
    "user.logout": "Logged out",
    "user.profile_updated": "Updated profile",
    "user.password_reset_requested": "Requested a password reset",
    "user.password_changed": "Changed password",
    "bill.created": "Added a bill",
    "bill.updated": "Updated a bill",
    "bill.deleted": "Deleted a bill",
    "bill.paid": "Paid a bill",
    "bill.next_occurrence_created": "Recurring bill rolled to next period",
    "expense.logged": "Logged spending",
    "expense.deleted": "Deleted an expense",
    "savings.goal_created": "Created a savings goal",
    "savings.goal_updated": "Updated a savings goal",
    "savings.goal_deleted": "Deleted a savings goal",
    "savings.contribution_added": "Added to savings",
    "savings.goal_achieved": "Hit a savings goal 🎉",
    "debt.created": "Added a debt",
    "debt.updated": "Updated a debt",
    "debt.deleted": "Deleted a debt",
    "debt.payment_recorded": "Recorded a debt payment",
    "debt.paid_off": "Paid off a debt 🎉",
    "income.recorded": "Recorded a paycheck",
    "income.deleted": "Deleted a paycheck",
}


def _friendly_event(ev: str) -> str:
    return _EVENT_FRIENDLY.get(ev, ev.replace(".", " ").replace("_", " ").title())


def _status_tag(status: str) -> str:
    tag_class = {
        "pending": "tag tag-yellow",
        "sent": "tag tag-green",
        "failed": "tag tag-red",
        "cancelled": "tag tag-gray",
    }.get(status, "tag")
    return f'<span class="{tag_class}">{e(status)}</span>'


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

@router.route("GET", "/notifications")
@auth_required
def notifications_page(req: Request) -> Response:
    items = notif_svc.list_for_user(req.user["id"])
    csrf = e(req.session["csrf_token"])

    rows = []
    for n in items:
        actions = ""
        if n["status"] == "pending":
            actions = (
                f'<form method="post" action="/notifications/{n["id"]}/dismiss" '
                f'class="inline-form">'
                f'<input type="hidden" name="csrf_token" value="{csrf}">'
                f'<button type="submit" class="secondary small">Dismiss</button>'
                f'</form>'
            )
        err_text = ""
        if n.get("last_error"):
            err_text = f'<br><small class="muted">error: {e(n["last_error"])}</small>'
        rows.append(
            f'<tr>'
            f'<td>{_status_tag(n["status"])}</td>'
            f'<td>{e(n["type"])}</td>'
            f'<td><strong>{e(n["subject"])}</strong>{err_text}</td>'
            f'<td class="muted">{e(str(n["scheduled_for"]))}</td>'
            f'<td>{e(str(n["sent_at"] or ""))}</td>'
            f'<td class="actions">{actions}</td>'
            f'</tr>'
        )
    rows_html = "\n".join(rows) or (
        '<tr><td colspan="6" class="muted">No notifications yet — '
        'they show up here as bills come due, paydays land, and the system '
        'sends reminders.</td></tr>'
    )

    return html(render(
        "notifications.html",
        title="Notifications",
        nav=nav_html("notifications"),
        csrf_token=csrf,
        rows=rows_html,
        error_block=error_block(""),
    ))


@router.route("POST", "/notifications/<int:notif_id>/dismiss")
@auth_required
@csrf_protect
def dismiss_html(req: Request) -> Response:
    notif_svc.cancel(req.user["id"], req.path_params["notif_id"])
    return redirect("/notifications")


@router.route("GET", "/activity")
@auth_required
def activity_page(req: Request) -> Response:
    events = recent_events(req.user["id"], limit=100)
    rows = []
    for ev in events:
        payload = ev.get("payload") or {}
        # Render payload as a small key=value list, hidden behind a details
        # element so the main feed stays clean.
        if payload:
            kv = ", ".join(f"{e(str(k))}={e(str(v))}" for k, v in payload.items())
            details = f' <details><summary class="muted small">details</summary><code>{kv}</code></details>'
        else:
            details = ""
        rows.append(
            f'<li>'
            f'<span class="muted small">{e(str(ev["created_at"]))}</span> '
            f'<strong>{e(_friendly_event(ev["event_type"]))}</strong>'
            f'{details}'
            f'</li>'
        )
    rows_html = "\n".join(rows) or '<li class="muted">No activity yet.</li>'

    return html(render(
        "activity.html",
        title="Activity",
        nav=nav_html("activity"),
        rows=rows_html,
    ))


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------

@router.route("GET", "/api/v1/notifications")
@auth_required
def api_list(req: Request) -> Response:
    try:
        limit = max(1, min(200, int(req.get_one("limit", "50"))))
    except ValueError:
        limit = 50
    items = notif_svc.list_for_user(req.user["id"], limit=limit)
    return json_response({"items": [
        {
            "id": n["id"],
            "type": n["type"],
            "subject": n["subject"],
            "body": n["body"],
            "scheduled_for": str(n["scheduled_for"]),
            "sent_at": str(n["sent_at"]) if n.get("sent_at") else None,
            "status": n["status"],
            "last_error": n.get("last_error"),
            "reference_type": n.get("reference_type"),
            "reference_id": n.get("reference_id"),
            "created_at": str(n.get("created_at") or ""),
        } for n in items
    ]})


@router.route("POST", "/api/v1/notifications/<int:notif_id>/dismiss")
@auth_required
@csrf_protect
def api_dismiss(req: Request) -> Response:
    ok = notif_svc.cancel(req.user["id"], req.path_params["notif_id"])
    if not ok:
        raise HTTPError(404, "Notification not found or already finalized.")
    return json_response({"ok": True})


@router.route("GET", "/api/v1/activity")
@auth_required
def api_activity(req: Request) -> Response:
    try:
        limit = max(1, min(200, int(req.get_one("limit", "50"))))
    except ValueError:
        limit = 50
    events = recent_events(req.user["id"], limit=limit)
    return json_response({"items": [
        {
            "id": ev["id"],
            "event_type": ev["event_type"],
            "friendly": _friendly_event(ev["event_type"]),
            "payload": ev.get("payload") or {},
            "created_at": str(ev["created_at"]),
        } for ev in events
    ]})
