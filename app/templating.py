"""
Minimal template engine.

We use `string.Template` for variable substitution. To get loops we render
list items in Python and substitute the joined result as a single `$rows`
variable. This keeps templates dumb and the engine zero-magic.

Templates live in app/templates/. A `base.html` exists; pages substitute
their content into `$content` and the page title into `$title`.
"""
from __future__ import annotations

from html import escape as _escape
from pathlib import Path
from string import Template
from typing import Any

from app.config import settings

_TEMPLATE_DIR = settings.PROJECT_ROOT / "app" / "templates"


def _load(name: str) -> Template:
    """Load a named template file. Templates are cached only in DEBUG=False."""
    path = _TEMPLATE_DIR / name
    return Template(path.read_text(encoding="utf-8"))


def e(value: Any) -> str:
    """HTML-escape any value. Use this on every user-supplied string."""
    if value is None:
        return ""
    return _escape(str(value), quote=True)


def render(template_name: str, **context: Any) -> str:
    """
    Render a template by substituting $variables.

    Pass `_layout='base.html'` (or omit — base.html is the default) to wrap
    the rendered content in a layout. Pass `_layout=None` to skip wrapping.
    """
    layout = context.pop("_layout", "base.html")
    raw = _load(template_name).safe_substitute(context)
    if layout is None:
        return raw
    layout_context = {
        "title": context.get("title", "Payday Tracker"),
        "flash": context.get("flash", ""),
        "user_email": context.get("user_email", ""),
        "nav": context.get("nav", ""),
        "body_class": context.get("body_class", ""),
        "content": raw,
    }
    return _load(layout).safe_substitute(layout_context)


def error_block(message: str) -> str:
    """Render an error banner, or empty string if no message."""
    return f'<div class="alert alert-error">{e(message)}</div>' if message else ""


def info_block(message: str) -> str:
    """Render an info/success banner, or empty string if no message."""
    return f'<div class="alert alert-info">{e(message)}</div>' if message else ""


def nav_html(active: str = "") -> str:
    """
    Render the navigation partial. `active` is the slug of the current section
    (e.g. 'bills') used to mark the active nav item.
    """
    try:
        tmpl = _load("_nav.html")
    except FileNotFoundError:
        return ""
    return tmpl.safe_substitute({
        "active_dashboard": "active" if active == "dashboard" else "",
        "active_bills": "active" if active == "bills" else "",
        "active_expenses": "active" if active == "expenses" else "",
        "active_savings": "active" if active == "savings" else "",
        "active_debts": "active" if active == "debts" else "",
        "active_income": "active" if active == "income" else "",
        "active_notifications": "active" if active == "notifications" else "",
        "active_activity": "active" if active == "activity" else "",
        "active_profile": "active" if active == "profile" else "",
    })


def render_rows(template_name: str, items: list[dict]) -> str:
    """
    Render a row template once per item and return the joined HTML.

    Used when a parent template needs a list of repeated children
    (e.g. bill rows, goal cards). Pass the result as the parent's $rows.
    """
    if not items:
        return ""
    tmpl = _load(template_name)
    return "".join(
        tmpl.safe_substitute({k: e(v) for k, v in item.items()})
        for item in items
    )
