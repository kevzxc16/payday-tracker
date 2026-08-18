"""
HTTP server bootstrap.

Wires up:
- BaseHTTPRequestHandler subclass that converts requests to Request objects
- routing dispatch
- session loading middleware
- error → HTTP response mapping
- static file serving from app/static/
"""
from __future__ import annotations

import logging
import mimetypes
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app.config import settings
from app.http_utils import HTTPError, Request, Response, html
from app.middleware import load_session
from app.router import router
from app.templating import e

log = logging.getLogger("payday_tracker")

_STATIC_DIR = settings.PROJECT_ROOT / "app" / "static"

# Import the routes that register themselves with the router on import.
# (Phase 2 will add more imports here for bills, expenses, savings, debts.)
from app import auth  # noqa: F401  -- side effect: registers /signup, /login, etc.
from app import routes  # noqa: F401  -- registers dashboard, etc.


class Handler(BaseHTTPRequestHandler):
    """Bridges Python's stdlib HTTP server to our Router."""

    server_version = "PaydayTracker/0.1"

    # Override the default access log format to keep it readable.
    def log_message(self, fmt, *args):
        log.info("%s - %s", self.address_string(), fmt % args)

    # All verbs go through one dispatcher.
    def do_GET(self):  self._dispatch()
    def do_POST(self): self._dispatch()
    def do_PUT(self):  self._dispatch()
    def do_PATCH(self): self._dispatch()
    def do_DELETE(self): self._dispatch()

    def _dispatch(self):
        try:
            # Health check: useful for the hosting platform and uptime checks.
            if self.path.split("?", 1)[0] == "/healthz":
                self._send_response(Response(
                    status=200,
                    body=b'{"status":"ok"}',
                    headers={"Content-Type": "application/json"},
                ))
                return

            # Static files: shortcut, no router involvement.
            if self.path.startswith("/static/"):
                self._serve_static()
                return

            req = Request.from_handler(self)
            load_session(req)

            match = router.resolve(req.method, req.path)
            if match is None:
                self._send_response(_not_found(req))
                return
            handler, params = match
            req.path_params = params
            resp = handler(req)
            self._send_response(resp)

        except HTTPError as exc:
            self._send_response(_error_response(exc, self.path))
        except Exception as exc:  # noqa: BLE001
            log.error("unhandled exception: %s", exc)
            log.error(traceback.format_exc())
            self._send_response(_server_error(exc))

    # ---------- helpers ----------
    def _send_response(self, resp: Response):
        self.send_response(resp.status)
        for k, v in resp.headers.items():
            self.send_header(k, v)
        for cookie in resp.cookies:
            self.send_header("Set-Cookie", cookie)
        if "Content-Length" not in resp.headers:
            self.send_header("Content-Length", str(len(resp.body)))
        self.end_headers()
        if resp.body:
            self.wfile.write(resp.body)

    def _serve_static(self):
        # Strip /static/ prefix and refuse traversal.
        rel = self.path[len("/static/"):].split("?")[0]
        target = (_STATIC_DIR / rel).resolve()
        try:
            target.relative_to(_STATIC_DIR.resolve())
        except ValueError:
            self._send_response(Response(status=403, body=b"Forbidden"))
            return
        if not target.is_file():
            self._send_response(Response(status=404, body=b"Not found"))
            return
        mime, _ = mimetypes.guess_type(target.name)
        body = target.read_bytes()
        self._send_response(Response(
            status=200,
            body=body,
            headers={"Content-Type": mime or "application/octet-stream"},
        ))


def _not_found(req: Request) -> Response:
    if req.wants_json():
        return Response(
            status=404,
            body=b'{"error":"Not found"}',
            headers={"Content-Type": "application/json"},
        )
    return html(
        "<!doctype html><h1>404</h1><p>Page not found.</p>"
        "<p><a href='/'>Home</a></p>",
        status=404,
    )


def _error_response(exc: HTTPError, path: str) -> Response:
    if path.startswith("/api/"):
        import json
        return Response(
            status=exc.status,
            body=json.dumps({"error": exc.message}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
    return html(
        f"<!doctype html><h1>{exc.status}</h1><p>{e(exc.message)}</p>"
        "<p><a href='/'>Home</a></p>",
        status=exc.status,
    )


def _server_error(exc: Exception) -> Response:
    if settings.DEBUG:
        body = (
            "<!doctype html><h1>500 Internal Server Error</h1>"
            f"<pre>{e(traceback.format_exc())}</pre>"
        )
    else:
        body = "<!doctype html><h1>500</h1><p>Something went wrong.</p>"
    return html(body, status=500)


def configure_logging():
    logging.basicConfig(
        level=logging.DEBUG if settings.DEBUG else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def serve():
    """Start the HTTP server. Blocks until interrupted."""
    configure_logging()
    address = (settings.HOST, settings.PORT)
    httpd = ThreadingHTTPServer(address, Handler)
    log.info("Listening on http://%s:%d (DEBUG=%s)",
             settings.HOST, settings.PORT, settings.DEBUG)

    # Background scheduler — handles notification generation, email dispatch,
    # and session housekeeping. Daemon thread, so it stops when the process
    # exits even if we don't explicitly call stop().
    from app.scheduler import BackgroundScheduler
    scheduler_interval = _int_env("SCHEDULER_INTERVAL_SECONDS", 60)
    scheduler = BackgroundScheduler(interval_seconds=scheduler_interval)
    scheduler.start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down…")
    finally:
        scheduler.stop()
        httpd.server_close()


def _int_env(name: str, default: int) -> int:
    """Read an int env var with a fallback. Kept local — no need in config."""
    import os
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
