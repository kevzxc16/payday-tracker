"""
Authentication routes: signup, login, logout, password reset.

All routes are registered against the global `router` from app.router.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone

from app import db
from app.config import settings
from app.http_utils import HTTPError, Request, Response, html, json_response, redirect
from app.middleware import csrf_protect
from app.router import router
from app.security import hash_password, random_token, verify_password
from app.sessions import COOKIE_NAME, create_session, destroy_session
from app.templating import e, error_block, info_block, render

log = logging.getLogger("payday_tracker.auth")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PAY_SCHEDULES = {"weekly", "biweekly", "monthly"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_signup(form: dict) -> dict:
    """Validate signup form. Returns cleaned values or raises HTTPError(400)."""
    email = (form.get("email") or "").strip().lower()
    password = form.get("password") or ""
    confirm = form.get("password_confirm") or ""
    pay_schedule = (form.get("pay_schedule") or "").strip().lower()
    first_payday = (form.get("first_payday") or "").strip()

    errors = []
    if not EMAIL_RE.match(email):
        errors.append("Please enter a valid email address.")
    if len(password) < 8:
        errors.append("Password must be at least 8 characters.")
    if password != confirm:
        errors.append("Passwords don't match.")
    if pay_schedule not in PAY_SCHEDULES:
        errors.append("Pay schedule must be weekly, biweekly, or monthly.")
    try:
        first_payday_dt = date.fromisoformat(first_payday)
    except ValueError:
        first_payday_dt = None
        errors.append("First payday must be a valid date (YYYY-MM-DD).")

    if errors:
        raise HTTPError(400, " ".join(errors))

    return {
        "email": email,
        "password": password,
        "pay_schedule": pay_schedule,
        "first_payday": first_payday_dt.isoformat(),
    }


def _render_signup(error: str = "", status: int = 200) -> Response:
    return html(
        render("signup.html", title="Sign up", body_class="auth", error_block=error_block(error)),
        status=status,
    )


def _render_login(error: str = "", next_url: str = "/dashboard",
                  status: int = 200) -> Response:
    return html(
        render("login.html", title="Log in", body_class="auth",
               error_block=error_block(error), next=e(next_url)),
        status=status,
    )


# ---------------------------------------------------------------------------
# Signup
# ---------------------------------------------------------------------------

@router.route("GET", "/signup")
def signup_form(req: Request) -> Response:
    return _render_signup()


@router.route("POST", "/signup")
def signup_submit(req: Request) -> Response:
    form = req.form() if not req.wants_json() else (req.json_body() or {})
    try:
        clean = _validate_signup(form)
    except HTTPError as exc:
        if req.wants_json():
            raise
        return _render_signup(error=exc.message, status=400)

    # Check email uniqueness.
    existing = db.query_one("SELECT id FROM users WHERE email = ?", (clean["email"],))
    if existing is not None:
        msg = "That email is already registered."
        if req.wants_json():
            raise HTTPError(409, msg)
        return _render_signup(error=msg, status=409)

    pw_hash, pw_salt = hash_password(clean["password"])

    with db.transaction():
        cur = db.execute(
            """
            INSERT INTO users
                (email, password_hash, password_salt, pay_schedule, first_payday)
            VALUES (?, ?, ?, ?, ?)
            """,
            (clean["email"], pw_hash, pw_salt,
             clean["pay_schedule"], clean["first_payday"]),
        )
        user_id = cur.lastrowid
        db.execute(
            "INSERT INTO activity_logs (user_id, event_type, payload) VALUES (?, ?, ?)",
            (user_id, "user.signup", json.dumps({"email": clean["email"]})),
        )

    session = create_session(user_id)

    # Queue a welcome email — picked up by the next scheduler tick.
    try:
        from app.notifications import send_welcome
        send_welcome(user_id, clean["email"])
    except Exception:  # noqa: BLE001 — don't fail signup if queuing fails
        log.exception("Failed to queue welcome notification")

    if req.wants_json():
        resp = json_response({"ok": True, "user_id": user_id}, status=201)
    else:
        resp = redirect("/dashboard")
    resp.set_cookie(
        COOKIE_NAME,
        session["token"],
        max_age=settings.SESSION_LIFETIME_DAYS * 86400,
        secure=not settings.DEBUG,
    )
    return resp


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

@router.route("GET", "/login")
def login_form(req: Request) -> Response:
    return _render_login(next_url=req.get_one("next", "/dashboard"))


@router.route("POST", "/login")
def login_submit(req: Request) -> Response:
    form = req.form() if not req.wants_json() else (req.json_body() or {})
    email = (form.get("email") or "").strip().lower()
    password = form.get("password") or ""
    next_url = form.get("next") or "/dashboard"

    user = db.query_one("SELECT * FROM users WHERE email = ?", (email,))
    # Always run the hasher even on a missing user — prevents timing-based
    # email enumeration.
    if user is None:
        hash_password(password)
        msg = "Invalid email or password."
        if req.wants_json():
            raise HTTPError(401, msg)
        return _render_login(error=msg, next_url=next_url, status=401)

    if not verify_password(password, user["password_hash"], user["password_salt"]):
        msg = "Invalid email or password."
        if req.wants_json():
            raise HTTPError(401, msg)
        return _render_login(error=msg, next_url=next_url, status=401)

    session = create_session(user["id"])
    db.execute(
        "INSERT INTO activity_logs (user_id, event_type, payload) VALUES (?, ?, ?)",
        (user["id"], "user.login", "{}"),
    )

    # Only allow same-origin redirects — path must start with / and not //.
    safe_next = next_url if (next_url.startswith("/") and not next_url.startswith("//")) else "/dashboard"

    if req.wants_json():
        resp = json_response({"ok": True})
    else:
        resp = redirect(safe_next)
    resp.set_cookie(
        COOKIE_NAME,
        session["token"],
        max_age=settings.SESSION_LIFETIME_DAYS * 86400,
        secure=not settings.DEBUG,
    )
    return resp


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------

@router.route("POST", "/logout")
@csrf_protect
def logout(req: Request) -> Response:
    if req.session:
        destroy_session(req.session["token"])
    if req.wants_json():
        resp = json_response({"ok": True})
    else:
        resp = redirect("/login")
    resp.clear_cookie(COOKIE_NAME)
    return resp


# ---------------------------------------------------------------------------
# Password reset — request a reset link
# ---------------------------------------------------------------------------

@router.route("GET", "/forgot-password")
def forgot_form(req: Request) -> Response:
    return html(render(
        "forgot_password.html",
        title="Forgot password",
        body_class="auth",
        message_block="",
    ))


@router.route("POST", "/forgot-password")
def forgot_submit(req: Request) -> Response:
    form = req.form() if not req.wants_json() else (req.json_body() or {})
    email = (form.get("email") or "").strip().lower()

    # Always respond the same way so we don't reveal which emails exist.
    generic_msg = (
        "If that email exists, we've queued a password reset link. "
        "Check your inbox in a few minutes."
    )

    user = db.query_one("SELECT id, email FROM users WHERE email = ?", (email,))
    if user is not None:
        token = random_token()
        expires_at = (_now() + timedelta(hours=1)).replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")
        reset_url = f"{settings.BASE_URL}/reset-password?token={token}"
        body = (
            "Hi,\n\n"
            "Someone (hopefully you) asked to reset your Payday Tracker password.\n"
            f"Use this link within 1 hour to choose a new password:\n\n{reset_url}\n\n"
            "If you didn't ask for this, you can ignore this email."
        )
        with db.transaction():
            db.execute(
                "INSERT INTO password_resets (token, user_id, expires_at) VALUES (?, ?, ?)",
                (token, user["id"], expires_at),
            )
            db.execute(
                """
                INSERT INTO notifications
                    (user_id, type, subject, body, scheduled_for,
                     reference_type, reference_id)
                VALUES (?, 'password_reset', 'Reset your Payday Tracker password',
                        ?, CURRENT_TIMESTAMP, 'password_reset', NULL)
                """,
                (user["id"], body),
            )
            db.execute(
                "INSERT INTO activity_logs (user_id, event_type, payload) VALUES (?, ?, ?)",
                (user["id"], "user.password_reset_requested", "{}"),
            )

    if req.wants_json():
        return json_response({"ok": True, "message": generic_msg})
    return html(render(
        "forgot_password.html",
        title="Forgot password",
        body_class="auth",
        message_block=info_block(generic_msg),
    ))


# ---------------------------------------------------------------------------
# Password reset — consume the token
# ---------------------------------------------------------------------------

@router.route("GET", "/reset-password")
def reset_form(req: Request) -> Response:
    token = req.get_one("token", "")
    if not _reset_token_valid(token):
        return html(
            render("reset_password.html",
                   title="Reset password",
                   body_class="auth",
                   token="",
                   error_block=error_block("This reset link is invalid or expired."),
                   message_block=""),
            status=400,
        )
    return html(render(
        "reset_password.html",
        title="Reset password",
        body_class="auth",
        token=e(token),
        error_block="",
        message_block="",
    ))


@router.route("POST", "/reset-password")
def reset_submit(req: Request) -> Response:
    form = req.form() if not req.wants_json() else (req.json_body() or {})
    token = (form.get("token") or "").strip()
    password = form.get("password") or ""
    confirm = form.get("password_confirm") or ""

    if not _reset_token_valid(token):
        msg = "This reset link is invalid or expired."
        if req.wants_json():
            raise HTTPError(400, msg)
        return html(
            render("reset_password.html",
                   title="Reset password",
                   body_class="auth",
                   token="",
                   error_block=error_block(msg),
                   message_block=""),
            status=400,
        )
    if len(password) < 8 or password != confirm:
        msg = "Password must be at least 8 characters and match confirmation."
        if req.wants_json():
            raise HTTPError(400, msg)
        return html(
            render("reset_password.html",
                   title="Reset password",
                   body_class="auth",
                   token=e(token),
                   error_block=error_block(msg),
                   message_block=""),
            status=400,
        )

    reset = db.query_one(
        "SELECT user_id FROM password_resets WHERE token = ?", (token,)
    )
    pw_hash, pw_salt = hash_password(password)

    with db.transaction():
        db.execute(
            """
            UPDATE users SET password_hash = ?, password_salt = ?,
                             updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (pw_hash, pw_salt, reset["user_id"]),
        )
        db.execute(
            "UPDATE password_resets SET used_at = CURRENT_TIMESTAMP WHERE token = ?",
            (token,),
        )
        # Invalidate all sessions on password change.
        db.execute("DELETE FROM sessions WHERE user_id = ?", (reset["user_id"],))
        db.execute(
            "INSERT INTO activity_logs (user_id, event_type, payload) VALUES (?, ?, ?)",
            (reset["user_id"], "user.password_changed", "{}"),
        )

    if req.wants_json():
        return json_response({"ok": True})
    return html(render(
        "reset_password.html",
        title="Reset password",
        body_class="auth",
        token="",
        error_block="",
        message_block=info_block("Your password has been updated. You can log in now."),
    ))


def _reset_token_valid(token: str) -> bool:
    """Check whether a password reset token is unused and unexpired."""
    if not token:
        return False
    row = db.query_one(
        """
        SELECT user_id, expires_at, used_at
        FROM password_resets WHERE token = ?
        """,
        (token,),
    )
    if row is None or row["used_at"] is not None:
        return False
    expires = row["expires_at"]
    if isinstance(expires, str):
        expires = datetime.fromisoformat(expires.replace(" ", "T"))
    if expires.tzinfo is not None:
        expires = expires.replace(tzinfo=None)
    return expires >= _now().replace(tzinfo=None)
