"""
HTTP request/response abstractions used by the router and handlers.

We wrap BaseHTTPRequestHandler with friendlier Request/Response objects so
handlers don't deal with raw rfile/wfile.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse


@dataclass
class Request:
    """A parsed HTTP request, populated by the router before dispatch."""

    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    body: bytes
    cookies: dict[str, str]
    # Filled in later by the router/middleware:
    path_params: dict[str, str] = field(default_factory=dict)
    user: Optional[dict] = None
    session: Optional[dict] = None

    @classmethod
    def from_handler(cls, handler) -> "Request":
        """Build a Request from a BaseHTTPRequestHandler instance."""
        parsed = urlparse(handler.path)
        query = parse_qs(parsed.query, keep_blank_values=True)

        # Read body if Content-Length is present.
        length = int(handler.headers.get("Content-Length", "0") or "0")
        body = handler.rfile.read(length) if length > 0 else b""

        # Headers: BaseHTTPRequestHandler exposes them as a Message object.
        headers = {k.lower(): v for k, v in handler.headers.items()}

        # Parse cookies.
        cookies: dict[str, str] = {}
        raw_cookie = headers.get("cookie", "")
        if raw_cookie:
            sc = SimpleCookie()
            sc.load(raw_cookie)
            cookies = {k: m.value for k, m in sc.items()}

        return cls(
            method=handler.command.upper(),
            path=parsed.path,
            query=query,
            headers=headers,
            body=body,
            cookies=cookies,
        )

    def form(self) -> dict[str, str]:
        """Parse a urlencoded form body into a flat dict (last value wins)."""
        ctype = self.headers.get("content-type", "")
        if "application/x-www-form-urlencoded" not in ctype:
            return {}
        parsed = parse_qs(self.body.decode("utf-8"), keep_blank_values=True)
        return {k: v[-1] for k, v in parsed.items()}

    def json_body(self) -> Any:
        """Parse a JSON body. Raises ValueError on malformed input."""
        if not self.body:
            return None
        try:
            return json.loads(self.body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc

    def get_one(self, key: str, default: str = "") -> str:
        """Get a single query string value (last one wins)."""
        values = self.query.get(key)
        return values[-1] if values else default

    def wants_json(self) -> bool:
        """True if the client asked for JSON or hit an /api/ route."""
        if self.path.startswith("/api/"):
            return True
        accept = self.headers.get("accept", "")
        return "application/json" in accept and "text/html" not in accept


@dataclass
class Response:
    """An HTTP response. Handlers either return one of these or a string."""

    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    cookies: list[str] = field(default_factory=list)  # raw Set-Cookie values

    def set_cookie(
        self,
        name: str,
        value: str,
        *,
        max_age: Optional[int] = None,
        path: str = "/",
        http_only: bool = True,
        secure: bool = False,
        same_site: str = "Lax",
    ) -> None:
        """Append a Set-Cookie header value."""
        parts = [f"{name}={value}", f"Path={path}", f"SameSite={same_site}"]
        if max_age is not None:
            parts.append(f"Max-Age={max_age}")
        if http_only:
            parts.append("HttpOnly")
        if secure:
            parts.append("Secure")
        self.cookies.append("; ".join(parts))

    def clear_cookie(self, name: str, path: str = "/") -> None:
        """Tell the browser to discard the cookie."""
        self.cookies.append(f"{name}=; Path={path}; Max-Age=0; HttpOnly; SameSite=Lax")


# ---------------------------------------------------------------------------
# Convenience constructors
# ---------------------------------------------------------------------------

def html(body: str, status: int = 200) -> Response:
    """Build an HTML response."""
    return Response(
        status=status,
        body=body.encode("utf-8"),
        headers={"Content-Type": "text/html; charset=utf-8"},
    )


def json_response(data: Any, status: int = 200) -> Response:
    """Build a JSON response."""
    return Response(
        status=status,
        body=json.dumps(data, default=str).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


def redirect(location: str, status: int = 302) -> Response:
    """Build a redirect response."""
    return Response(status=status, headers={"Location": location})


def text(body: str, status: int = 200) -> Response:
    """Plain-text response."""
    return Response(
        status=status,
        body=body.encode("utf-8"),
        headers={"Content-Type": "text/plain; charset=utf-8"},
    )


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class HTTPError(Exception):
    """Raise from a handler to short-circuit with an HTTP error response."""

    def __init__(self, status: int, message: str = ""):
        self.status = status
        self.message = message or self._default_message(status)
        super().__init__(self.message)

    @staticmethod
    def _default_message(status: int) -> str:
        return {
            400: "Bad Request",
            401: "Unauthorized",
            403: "Forbidden",
            404: "Not Found",
            405: "Method Not Allowed",
            409: "Conflict",
            422: "Unprocessable Entity",
            500: "Internal Server Error",
        }.get(status, "Error")
