"""
Routes integration tests.

These start the real HTTP server in a background thread on an ephemeral port,
make real HTTP calls via urllib, and verify behavior end-to-end. This
catches issues that pure-function tests miss: cookie handling, redirects,
CSRF protection, route registration.
"""
from __future__ import annotations

import http.cookiejar
import json
import re
import socket
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer

from tests._helpers import TempDBTestCase


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class _ServerCtx:
    """Run the app's HTTP server in a thread on a random port."""

    def __init__(self) -> None:
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self):
        # Imported lazily so DB path swap in TempDBTestCase has already happened.
        from app.server import Handler
        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        # Wait for the server to be reachable.
        for _ in range(50):
            try:
                urllib.request.urlopen(self.base + "/login", timeout=1).read()
                break
            except urllib.error.URLError:
                time.sleep(0.05)
        return self

    def __exit__(self, *args):
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread:
            self._thread.join(timeout=2)


class _Client:
    """Tiny HTTP client with cookie jar + form/JSON helpers."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj)
        )
        # A second opener that doesn't follow redirects for testing 302s.
        class _NoRedir(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **kw): return None
        self.opener_no_redir = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj), _NoRedir(),
        )

    def get(self, path, *, headers=None, follow=True):
        req = urllib.request.Request(self.base + path, headers=headers or {})
        try:
            return (self.opener if follow else self.opener_no_redir).open(req)
        except urllib.error.HTTPError as e:
            return e

    def post_form(self, path, data, *, follow=False):
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(
            self.base + path, data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            return (self.opener if follow else self.opener_no_redir).open(req)
        except urllib.error.HTTPError as e:
            return e

    def post_json(self, path, data, method="POST", csrf=None):
        body = json.dumps(data).encode()
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if csrf:
            headers["X-CSRF-Token"] = csrf
        req = urllib.request.Request(self.base + path, data=body, method=method,
                                      headers=headers)
        try:
            return self.opener.open(req)
        except urllib.error.HTTPError as e:
            return e


class RoutesTests(TempDBTestCase):
    """Auth flow + bill lifecycle + recurring rollover + JSON API parity."""

    def test_full_user_flow(self):
        with _ServerCtx() as srv:
            c = _Client(srv.base)
            today = date.today()

            # --- signup ---
            r = c.post_form("/signup", {
                "email": "u@example.com",
                "password": "hunter2222",
                "password_confirm": "hunter2222",
                "pay_schedule": "biweekly",
                "first_payday": today.isoformat(),
            })
            self.assertEqual(r.status, 302)
            self.assertEqual(r.headers.get("Location"), "/dashboard")

            # --- dashboard reachable ---
            r = c.get("/dashboard")
            self.assertEqual(r.status, 200)
            body = r.read().decode()
            self.assertIn("period-indicator", body)
            self.assertIn("This payday", body)

            # Grab a CSRF token from a page that has a form (the dashboard
            # no longer contains one — logout lives on the profile page).
            bills_page = c.get("/bills").read().decode()
            csrf = re.search(r'name="csrf_token" value="([^"]+)"', bills_page).group(1)

            # --- bill lifecycle: create → pay (creates next occurrence) ---
            r = c.post_form("/bills", {
                "csrf_token": csrf, "name": "Rent", "amount": "1200.00",
                "due_date": today.isoformat(), "is_recurring": "1",
                "recurrence": "monthly", "category": "Housing",
            })
            self.assertEqual(r.status, 302)

            r = c.get("/api/v1/bills", headers={"Accept": "application/json"})
            self.assertEqual(r.status, 200)
            bills = json.loads(r.read())["items"]
            self.assertEqual(len(bills), 1)
            rent_id = bills[0]["id"]

            # Mark paid
            r = c.post_form(f"/bills/{rent_id}/pay", {"csrf_token": csrf})
            self.assertEqual(r.status, 302)

            # Now there should be 2 bills (paid + new unpaid next month)
            r = c.get("/api/v1/bills", headers={"Accept": "application/json"})
            bills = json.loads(r.read())["items"]
            self.assertEqual(len(bills), 2)
            statuses = sorted(b["status"] for b in bills)
            self.assertEqual(statuses, ["paid", "unpaid"])

            # --- JSON API without CSRF rejected ---
            r = c.post_json("/api/v1/paychecks",
                            {"amount": "100", "received_on": today.isoformat()})
            self.assertEqual(r.status, 403)

            # With CSRF header it works
            r = c.post_json("/api/v1/paychecks",
                            {"amount": "100", "received_on": today.isoformat()},
                            csrf=csrf)
            self.assertEqual(r.status, 201)

            # --- unauthenticated API call ---
            c2 = _Client(srv.base)
            r = c2.get("/api/v1/dashboard", headers={"Accept": "application/json"})
            self.assertEqual(r.status, 401)

            # --- logout invalidates session ---
            r = c.post_form("/logout", {"csrf_token": csrf})
            self.assertEqual(r.status, 302)
            r = c.get("/dashboard", follow=False)
            self.assertEqual(r.status, 302)
            self.assertIn("/login", r.headers.get("Location"))

    def test_404(self):
        with _ServerCtx() as srv:
            c = _Client(srv.base)
            r = c.get("/does-not-exist")
            self.assertEqual(r.status, 404)

    def test_static_traversal_blocked(self):
        with _ServerCtx() as srv:
            c = _Client(srv.base)
            r = c.get("/static/../config.py")
            self.assertIn(r.status, (403, 404))
