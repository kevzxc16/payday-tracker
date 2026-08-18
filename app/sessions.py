"""
Session management.

Sessions are stored server-side in the `sessions` table. The client gets only
an opaque random token in an HttpOnly cookie. Each session also carries a
CSRF token used for state-changing requests.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from app import db
from app.config import settings
from app.security import random_token

COOKIE_NAME = "pdt_session"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_session(user_id: int) -> dict:
    """Create a new session row and return {token, csrf_token, expires_at}."""
    token = random_token()
    csrf = random_token()
    expires_at = _now() + timedelta(days=settings.SESSION_LIFETIME_DAYS)
    # Store as naive UTC string — UTC is implicit by convention across the DB.
    db.execute(
        """
        INSERT INTO sessions (token, user_id, csrf_token, expires_at)
        VALUES (?, ?, ?, ?)
        """,
        (token, user_id, csrf,
         expires_at.replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")),
    )
    return {"token": token, "csrf_token": csrf, "expires_at": expires_at}


def load_session(token: Optional[str]) -> Optional[dict]:
    """
    Look up a session token. Returns dict with user row + csrf_token, or None.

    Expired sessions are deleted lazily on the way through.
    """
    if not token:
        return None
    row = db.query_one(
        """
        SELECT s.token, s.csrf_token, s.expires_at, u.*
        FROM sessions s
        JOIN users u ON u.id = s.user_id
        WHERE s.token = ?
        """,
        (token,),
    )
    if row is None:
        return None
    # PARSE_DECLTYPES turns expires_at into a naive datetime. We treat it as UTC.
    expires = row["expires_at"]
    if isinstance(expires, str):
        expires = datetime.fromisoformat(expires.replace(" ", "T"))
    if expires.tzinfo is not None:
        expires = expires.replace(tzinfo=None)
    if expires < _now().replace(tzinfo=None):
        destroy_session(token)
        return None
    return row


def destroy_session(token: Optional[str]) -> None:
    """Delete a session row. No-op if the token is missing or unknown."""
    if not token:
        return
    db.execute("DELETE FROM sessions WHERE token = ?", (token,))


def purge_expired() -> int:
    """Delete all expired sessions. Returns count removed. Run periodically."""
    cur = db.execute(
        "DELETE FROM sessions WHERE expires_at < ?",
        (_now().replace(tzinfo=None).isoformat(sep=" ", timespec="seconds"),),
    )
    return cur.rowcount
