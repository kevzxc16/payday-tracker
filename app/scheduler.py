"""
Background scheduler.

A simple daemon thread that ticks at a fixed interval. Each tick:

1. Runs notification generation for all users (idempotent — safe to re-run).
2. Dispatches any pending notifications whose scheduled_for has passed.
3. Purges expired sessions and password reset tokens.

Stdlib-only: we use `threading.Thread` + `threading.Event` for cancellation.
We deliberately don't use `sched` because we want a simple "wake every N
seconds, do work, sleep" loop that responds to a stop signal mid-sleep.

The scheduler is started from `server.serve()` and stopped on shutdown.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta

from app import db
from app import notifications
from app import sessions

log = logging.getLogger("payday_tracker.scheduler")


class BackgroundScheduler:
    """
    Polling scheduler. Construct, start(), call stop() at shutdown.

    `interval_seconds` controls how often the worker wakes up. Default 60s
    is reasonable; tests can drop it to 1.
    """

    def __init__(self, interval_seconds: int = 60) -> None:
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_tick: datetime | None = None
        self._last_summary: dict | None = None

    # ------------------------------------------------------------------ public

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            t = threading.Thread(
                target=self._run, name="payday-scheduler", daemon=True,
            )
            self._thread = t
            t.start()
            log.info("Scheduler started (interval=%ss)", self.interval_seconds)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=timeout)
        log.info("Scheduler stopped")

    def tick_now(self) -> dict:
        """Run a single tick synchronously. Useful for tests."""
        return self._do_tick()

    def status(self) -> dict:
        return {
            "running": self._thread is not None and self._thread.is_alive(),
            "interval_seconds": self.interval_seconds,
            "last_tick": self._last_tick.isoformat() if self._last_tick else None,
            "last_summary": self._last_summary,
        }

    # ------------------------------------------------------------------ inner

    def _run(self) -> None:
        # Tick once immediately so a freshly-started server processes any
        # already-due work, then settle into the regular cadence.
        try:
            self._do_tick()
        except Exception:  # noqa: BLE001
            log.exception("Scheduler tick failed (will continue)")
        while not self._stop.is_set():
            if self._stop.wait(timeout=self.interval_seconds):
                break
            try:
                self._do_tick()
            except Exception:  # noqa: BLE001
                log.exception("Scheduler tick failed (will continue)")

    def _do_tick(self) -> dict:
        now = datetime.utcnow()
        # 1. Generation: enqueue any new notifications.
        gen = notifications.generate_for_all_users(on=now.date())
        # 2. Dispatch: send anything pending whose scheduled_for has passed.
        disp = notifications.dispatch_pending(now=now)
        # 3. Housekeeping.
        housekeeping = _housekeeping(now)

        summary = {
            "at": now.isoformat(timespec="seconds"),
            "generated": gen,
            "dispatched": disp,
            "housekeeping": housekeeping,
        }
        self._last_tick = now
        self._last_summary = summary
        log.info(
            "Tick: generated=%s dispatched=%s housekeeping=%s",
            gen, disp, housekeeping,
        )
        return summary


def _housekeeping(now: datetime) -> dict:
    """Purge expired sessions and old password-reset tokens."""
    purged_sessions = sessions.purge_expired() if hasattr(sessions, "purge_expired") else 0

    # Also purge unused password reset tokens older than 7 days.
    cutoff = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    res = db.execute(
        "DELETE FROM password_resets WHERE expires_at < ? OR "
        "(used_at IS NOT NULL AND used_at < ?)",
        (cutoff, cutoff),
    )
    purged_resets = res.rowcount

    return {
        "sessions_purged": purged_sessions,
        "password_resets_purged": purged_resets,
    }
