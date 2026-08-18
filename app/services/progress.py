"""
Activity log helpers — generic event log used across all resource modules.
"""
from __future__ import annotations

import json
from typing import Any

from app import db


def log_event(user_id: int, event_type: str, payload: Any = None) -> None:
    """Append a row to activity_logs. Safe to call from inside a transaction."""
    db.execute(
        "INSERT INTO activity_logs (user_id, event_type, payload) VALUES (?, ?, ?)",
        (user_id, event_type, json.dumps(payload or {}, default=str)),
    )


def recent_events(user_id: int, limit: int = 25) -> list[dict]:
    """Return the most recent activity_log rows for a user."""
    rows = db.query(
        """
        SELECT id, event_type, payload, created_at
        FROM activity_logs
        WHERE user_id = ?
        ORDER BY id DESC LIMIT ?
        """,
        (user_id, limit),
    )
    for r in rows:
        try:
            r["payload"] = json.loads(r["payload"]) if r["payload"] else {}
        except (json.JSONDecodeError, TypeError):
            r["payload"] = {}
    return rows
