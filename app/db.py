"""
Database layer.

Owns:
- one sqlite3 connection per thread (WAL mode, foreign keys ON)
- the full schema (created on startup if missing)
- small query helpers that return dicts instead of tuples

All money is stored as INTEGER cents to avoid floating-point bugs.
All dates are stored as ISO 8601 strings ("YYYY-MM-DD" or full datetime).
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from app.config import settings


# The default sqlite3 timestamp converter can't handle ISO strings with
# timezone offsets (e.g. "2026-07-11 12:00:00+00:00"). Register a forgiving
# replacement that does. Treat naive strings as UTC.
def _parse_timestamp(b: bytes) -> datetime:
    s = b.decode("utf-8").strip().replace(" ", "T")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        # Fall back to truncating fractional seconds we can't parse.
        return datetime.fromisoformat(s.split(".")[0])


sqlite3.register_converter("TIMESTAMP", _parse_timestamp)


# sqlite3 connections aren't thread-safe by default; give each thread its own.
_local = threading.local()


def get_conn() -> sqlite3.Connection:
    """Return this thread's sqlite connection, creating it if needed."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        Path(settings.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            settings.DB_PATH,
            detect_types=sqlite3.PARSE_DECLTYPES,
            isolation_level=None,  # autocommit; we use explicit BEGIN/COMMIT
        )
        conn.row_factory = sqlite3.Row  # rows behave like dicts
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        _local.conn = conn
    return conn


def _reset_connection() -> None:
    """Close + drop the current thread's connection. Used by tests."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        _local.conn = None


def query(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    """Run SELECT and return a list of dict rows."""
    rows = get_conn().execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def query_one(sql: str, params: Iterable[Any] = ()) -> dict | None:
    """Run SELECT and return the first row as a dict, or None."""
    row = get_conn().execute(sql, params).fetchone()
    return dict(row) if row is not None else None


def execute(sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
    """Run INSERT/UPDATE/DELETE. Returns the cursor (use .lastrowid, .rowcount)."""
    return get_conn().execute(sql, params)


def executemany(sql: str, seq_of_params: Iterable[Iterable[Any]]) -> sqlite3.Cursor:
    """Run the same statement against many parameter tuples."""
    return get_conn().executemany(sql, seq_of_params)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
#
# Designed with monetization in mind:
# - users.tier ('free' | 'premium') gates premium features
# - feature_flags table allows per-user beta flags without schema changes
# - activity_logs is generic so new event types add no columns

SCHEMA = """
-- ---------- accounts ----------
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash   TEXT NOT NULL,
    password_salt   TEXT NOT NULL,
    pay_schedule    TEXT NOT NULL
                    CHECK (pay_schedule IN ('weekly','biweekly','monthly')),
    first_payday    DATE NOT NULL,
    timezone        TEXT NOT NULL DEFAULT 'UTC',
    tier            TEXT NOT NULL DEFAULT 'free'
                    CHECK (tier IN ('free','premium')),
    display_name    TEXT,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    csrf_token  TEXT NOT NULL,
    expires_at  TIMESTAMP NOT NULL,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS password_resets (
    token       TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    expires_at  TIMESTAMP NOT NULL,
    used_at     TIMESTAMP,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- ---------- income ----------
CREATE TABLE IF NOT EXISTS paychecks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    amount_cents    INTEGER NOT NULL CHECK (amount_cents >= 0),
    received_on     DATE NOT NULL,
    note            TEXT,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_paychecks_user_date
    ON paychecks(user_id, received_on);

-- ---------- bills ----------
CREATE TABLE IF NOT EXISTS bills (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    name            TEXT NOT NULL,
    amount_cents    INTEGER NOT NULL CHECK (amount_cents >= 0),
    due_date        DATE NOT NULL,
    is_recurring    INTEGER NOT NULL DEFAULT 0,
    recurrence      TEXT CHECK (recurrence IN
                    ('weekly','biweekly','monthly','yearly') OR recurrence IS NULL),
    status          TEXT NOT NULL DEFAULT 'unpaid'
                    CHECK (status IN ('unpaid','paid','overdue','skipped')),
    paid_on         DATE,
    category        TEXT,
    notes           TEXT,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_bills_user_due ON bills(user_id, due_date);
CREATE INDEX IF NOT EXISTS idx_bills_status ON bills(user_id, status);

-- ---------- spending ----------
CREATE TABLE IF NOT EXISTS expenses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    amount_cents    INTEGER NOT NULL CHECK (amount_cents >= 0),
    category        TEXT NOT NULL,
    spent_on        DATE NOT NULL,
    description     TEXT,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_expenses_user_date
    ON expenses(user_id, spent_on);
CREATE INDEX IF NOT EXISTS idx_expenses_user_category
    ON expenses(user_id, category);

-- ---------- savings ----------
CREATE TABLE IF NOT EXISTS savings_goals (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL,
    name                TEXT NOT NULL,
    target_amount_cents INTEGER NOT NULL CHECK (target_amount_cents > 0),
    deadline            DATE,
    status              TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active','achieved','paused','cancelled')),
    notes               TEXT,
    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_goals_user ON savings_goals(user_id);

CREATE TABLE IF NOT EXISTS savings_contributions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id             INTEGER NOT NULL,
    amount_cents        INTEGER NOT NULL CHECK (amount_cents > 0),
    contributed_on      DATE NOT NULL,
    note                TEXT,
    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (goal_id) REFERENCES savings_goals(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_contrib_goal ON savings_contributions(goal_id);

-- ---------- debts ----------
CREATE TABLE IF NOT EXISTS debts (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id                     INTEGER NOT NULL,
    name                        TEXT NOT NULL,
    starting_balance_cents      INTEGER NOT NULL CHECK (starting_balance_cents >= 0),
    current_balance_cents       INTEGER NOT NULL CHECK (current_balance_cents >= 0),
    minimum_payment_cents       INTEGER NOT NULL CHECK (minimum_payment_cents >= 0),
    target_payoff_date          DATE,
    interest_rate_bps           INTEGER,  -- basis points; 1599 = 15.99%
    status                      TEXT NOT NULL DEFAULT 'active'
                                CHECK (status IN ('active','paid_off','closed')),
    notes                       TEXT,
    created_at                  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at                  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_debts_user ON debts(user_id);

CREATE TABLE IF NOT EXISTS debt_payments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    debt_id         INTEGER NOT NULL,
    amount_cents    INTEGER NOT NULL CHECK (amount_cents > 0),
    paid_on         DATE NOT NULL,
    note            TEXT,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (debt_id) REFERENCES debts(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_payments_debt ON debt_payments(debt_id);

-- ---------- progress / activity ----------
CREATE TABLE IF NOT EXISTS activity_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    event_type  TEXT NOT NULL,
    payload     TEXT,  -- JSON-encoded
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_activity_user_time
    ON activity_logs(user_id, created_at);

-- ---------- notifications ----------
CREATE TABLE IF NOT EXISTS notifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    type            TEXT NOT NULL,
    -- types: 'bill_due', 'savings_reminder', 'debt_due',
    -- 'period_checkin', 'password_reset', 'welcome'
    subject         TEXT NOT NULL,
    body            TEXT NOT NULL,
    scheduled_for   TIMESTAMP NOT NULL,
    sent_at         TIMESTAMP,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','sent','failed','cancelled')),
    last_error      TEXT,
    reference_type  TEXT,
    reference_id    INTEGER,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_notif_pending
    ON notifications(status, scheduled_for);
CREATE INDEX IF NOT EXISTS idx_notif_user
    ON notifications(user_id, created_at);

-- ---------- monetization plumbing ----------
CREATE TABLE IF NOT EXISTS feature_flags (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    flag_name   TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 0,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, flag_name),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
"""


def init_db() -> None:
    """Create all tables if they don't already exist. Safe to run repeatedly."""
    conn = get_conn()
    conn.executescript(SCHEMA)


def transaction():
    """
    Context manager for explicit transactions.

    Usage:
        with transaction():
            execute("INSERT ...")
            execute("UPDATE ...")
    """
    return _Transaction(get_conn())


class _Transaction:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
        return False  # never swallow exceptions
