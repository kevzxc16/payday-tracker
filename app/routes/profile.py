"""
Profile routes — view and update the user's pay schedule, display name, timezone.

HTML at /profile, JSON API at /api/v1/me.
"""
from __future__ import annotations

from datetime import date

from app import db
from app.http_utils import HTTPError, Request, Response, html, json_response, redirect
from app.middleware import auth_required, csrf_protect
from app.router import router
from app.services.progress import log_event
from app.templating import e, error_block, info_block, nav_html, render

PAY_SCHEDULES = {"weekly", "biweekly", "monthly"}


def _serialize_user(u: dict) -> dict:
    """Strip sensitive fields before returning a user record over the API."""
    return {
        "id": u["id"],
        "email": u["email"],
        "display_name": u.get("display_name") or "",
        "pay_schedule": u["pay_schedule"],
        "first_payday": u["first_payday"],
        "timezone": u.get("timezone") or "UTC",
        "tier": u["tier"],
        "created_at": str(u.get("created_at") or ""),
    }


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

@router.route("GET", "/profile")
@auth_required
def profile_page(req: Request) -> Response:
    return _render_profile(req)


@router.route("POST", "/profile")
@auth_required
@csrf_protect
def profile_update(req: Request) -> Response:
    form = req.form()
    try:
        _apply_update(req.user["id"], form)
    except HTTPError as exc:
        return _render_profile(req, error=exc.message, status=exc.status)
    return _render_profile(req, info="Profile updated.", refresh_user=True)


def _render_profile(req: Request, *, error: str = "", info: str = "",
                    status: int = 200, refresh_user: bool = False) -> Response:
    user = req.user
    if refresh_user:
        user = db.query_one("SELECT * FROM users WHERE id = ?", (user["id"],))
    return html(render(
        "profile.html",
        title="Profile",
        nav=nav_html("profile"),
        csrf_token=e(req.session["csrf_token"]),
        email=e(user["email"]),
        display_name=e(user.get("display_name") or ""),
        pay_schedule=e(user["pay_schedule"]),
        first_payday=e(user["first_payday"]),
        timezone=e(user.get("timezone") or "UTC"),
        tier=e(user["tier"]),
        weekly_selected="selected" if user["pay_schedule"] == "weekly" else "",
        biweekly_selected="selected" if user["pay_schedule"] == "biweekly" else "",
        monthly_selected="selected" if user["pay_schedule"] == "monthly" else "",
        error_block=error_block(error),
        info_block=info_block(info),
    ), status=status)


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------

@router.route("GET", "/api/v1/me")
@auth_required
def api_me(req: Request) -> Response:
    return json_response(_serialize_user(req.user))


@router.route("PUT", "/api/v1/me")
@auth_required
@csrf_protect
def api_me_update(req: Request) -> Response:
    try:
        payload = req.json_body() or {}
    except ValueError as exc:
        raise HTTPError(400, str(exc))
    _apply_update(req.user["id"], payload)
    updated = db.query_one("SELECT * FROM users WHERE id = ?", (req.user["id"],))
    return json_response(_serialize_user(updated))


# ---------------------------------------------------------------------------
# shared
# ---------------------------------------------------------------------------

def _apply_update(user_id: int, fields: dict) -> None:
    """Validate + apply profile updates. Raises HTTPError(400) on invalid input."""
    updates: dict[str, object] = {}

    if "display_name" in fields:
        name = (fields.get("display_name") or "").strip()
        if len(name) > 80:
            raise HTTPError(400, "Display name too long (max 80 chars).")
        updates["display_name"] = name or None

    if "pay_schedule" in fields:
        sched = (fields.get("pay_schedule") or "").strip().lower()
        if sched not in PAY_SCHEDULES:
            raise HTTPError(400, "Invalid pay schedule.")
        updates["pay_schedule"] = sched

    if "first_payday" in fields:
        raw = (fields.get("first_payday") or "").strip()
        try:
            date.fromisoformat(raw)
        except ValueError:
            raise HTTPError(400, "Invalid first payday date.")
        updates["first_payday"] = raw

    if "timezone" in fields:
        tz = (fields.get("timezone") or "UTC").strip()
        if len(tz) > 64:
            raise HTTPError(400, "Timezone string too long.")
        updates["timezone"] = tz

    if not updates:
        return

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    params = list(updates.values()) + [user_id]
    db.execute(
        f"UPDATE users SET {set_clause}, updated_at = CURRENT_TIMESTAMP "
        f"WHERE id = ?",
        params,
    )
    log_event(user_id, "user.profile_updated", {"fields": list(updates.keys())})
