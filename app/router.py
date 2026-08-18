"""
URL router.

Routes are registered with the @route decorator. Path patterns can include
typed parameters like /bills/<int:bill_id> or /goals/<str:slug>.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

from app.http_utils import HTTPError, Request, Response

Handler = Callable[[Request], Response]

# Map of "type" → (regex fragment, converter function).
_PARAM_TYPES: dict[str, tuple[str, Callable[[str], object]]] = {
    "int": (r"(\d+)", int),
    "str": (r"([^/]+)", str),
}

# Find <type:name> in patterns.
_PARAM_RE = re.compile(r"<(int|str):([a-zA-Z_][a-zA-Z0-9_]*)>")


@dataclass
class Route:
    method: str
    pattern: re.Pattern
    param_names: list[str]
    param_converters: list[Callable[[str], object]]
    handler: Handler


class Router:
    """Holds registered routes and resolves requests to handlers."""

    def __init__(self):
        self._routes: list[Route] = []

    def add(self, method: str, path_pattern: str, handler: Handler) -> None:
        """Register a single route."""
        regex, names, converters = self._compile(path_pattern)
        self._routes.append(
            Route(method.upper(), regex, names, converters, handler)
        )

    def route(self, method: str, path_pattern: str):
        """Decorator form: @router.route('GET', '/foo')"""
        def decorator(fn: Handler) -> Handler:
            self.add(method, path_pattern, fn)
            return fn
        return decorator

    def resolve(self, method: str, path: str) -> Optional[tuple[Handler, dict]]:
        """
        Find a handler matching this method+path.

        Returns (handler, path_params_dict) or None if no route matches.
        Raises HTTPError(405) if path matches a route but not the method.
        """
        method = method.upper()
        path_matched_any = False
        for r in self._routes:
            m = r.pattern.match(path)
            if not m:
                continue
            path_matched_any = True
            if r.method != method:
                continue
            params = {
                name: conv(m.group(i + 1))
                for i, (name, conv) in enumerate(zip(r.param_names, r.param_converters))
            }
            return r.handler, params
        if path_matched_any:
            raise HTTPError(405)
        return None

    @staticmethod
    def _compile(pattern: str):
        """Turn '/bills/<int:bill_id>' into a regex + param metadata."""
        names: list[str] = []
        converters: list[Callable[[str], object]] = []

        def repl(match):
            type_name, var_name = match.group(1), match.group(2)
            regex_fragment, conv = _PARAM_TYPES[type_name]
            names.append(var_name)
            converters.append(conv)
            return regex_fragment

        regex_str = "^" + _PARAM_RE.sub(repl, pattern) + "$"
        return re.compile(regex_str), names, converters


# Module-level singleton — all routes register against this.
router = Router()
