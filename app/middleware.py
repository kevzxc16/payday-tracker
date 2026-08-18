"""
Middleware: cross-cutting logic applied around handler invocation.

In this app, "middleware" is just helper decorators handlers can wear:
- @auth_required ensures a logged-in user
- @csrf_protect validates the CSRF token on state-changing requests
"""
from __future__ import annotations

from functools import wraps
from typing import Callable

from app import sessions as session_store
from app.http_utils import HTTPError, Request, Response, redirect
from app.sessions import COOKIE_NAME


def load_session(req: Request) -> None:
    """
    Populate req.session and req.user from the session cookie, if any.

    Always runs at the start of every request. Doesn't enforce auth.
    """
    token = req.cookies.get(COOKIE_NAME)
    session = session_store.load_session(token)
    if session is None:
        return
    # session is a merged row of sessions + users. Split into two views.
    req.session = {
        "token": session["token"],
        "csrf_token": session["csrf_token"],
        "expires_at": session["expires_at"],
    }
    # Strip session-specific keys to leave the user record.
    user = {k: v for k, v in session.items()
            if k not in {"token", "csrf_token", "expires_at"}}
    req.user = user


def auth_required(fn: Callable[[Request], Response]) -> Callable[[Request], Response]:
    """
    Decorator: reject the request if no user is logged in.

    HTML routes redirect to /login. API routes return 401 JSON.
    """
    @wraps(fn)
    def wrapper(req: Request) -> Response:
        if req.user is None:
            if req.wants_json():
                raise HTTPError(401, "Authentication required")
            return redirect(f"/login?next={req.path}")
        return fn(req)
    return wrapper


def csrf_protect(fn: Callable[[Request], Response]) -> Callable[[Request], Response]:
    """
    Decorator: require a valid CSRF token on POST/PUT/PATCH/DELETE.

    Token can come from form field `csrf_token` or header `X-CSRF-Token`.
    JSON API requests are exempt only when authenticated via Bearer token
    (not implemented in this phase — same-origin cookie use still needs CSRF).
    """
    UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}

    @wraps(fn)
    def wrapper(req: Request) -> Response:
        if req.method not in UNSAFE:
            return fn(req)
        if req.session is None:
            raise HTTPError(403, "No session")
        expected = req.session.get("csrf_token")
        provided = (
            req.headers.get("x-csrf-token")
            or req.form().get("csrf_token", "")
        )
        if not expected or not provided or expected != provided:
            raise HTTPError(403, "Invalid CSRF token")
        return fn(req)

    return wrapper
