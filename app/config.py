"""
Configuration loader.

Reads a `.env` file (if present) and environment variables, then exposes
them as a typed `settings` singleton. We don't use python-dotenv because
we're stdlib-only — a 20-line parser is enough.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Parse a simple KEY=VALUE .env file into os.environ (no overrides)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # Only set if not already in env — real env wins over .env file.
        os.environ.setdefault(key, value)


# Load .env from the project root (one directory above this file's parent).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_load_dotenv(_PROJECT_ROOT / ".env")


def _bool(name: str, default: bool = False) -> bool:
    """Parse a boolean-ish env var ('true', '1', 'yes', etc.)."""
    raw = os.environ.get(name, "").strip().lower()
    if raw == "":
        return default
    return raw in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    """All config values, frozen and typed."""

    HOST: str
    PORT: int
    DB_PATH: str
    SECRET_KEY: str
    SESSION_LIFETIME_DAYS: int
    SMTP_HOST: str
    SMTP_PORT: int
    SMTP_USERNAME: str
    SMTP_PASSWORD: str
    SMTP_FROM_NAME: str
    SMTP_FROM_EMAIL: str
    SMTP_USE_TLS: bool
    BASE_URL: str
    DEBUG: bool
    PROJECT_ROOT: Path


# Resolve DB_PATH relative to project root if it's not absolute.
_raw_db_path = os.environ.get("DB_PATH", "data/payday.db")
_db_path = Path(_raw_db_path)
if not _db_path.is_absolute():
    _db_path = _PROJECT_ROOT / _db_path

settings = Settings(
    HOST=os.environ.get("HOST", "127.0.0.1"),
    PORT=_int("PORT", 8000),
    DB_PATH=str(_db_path),
    SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-insecure-key"),
    SESSION_LIFETIME_DAYS=_int("SESSION_LIFETIME_DAYS", 30),
    SMTP_HOST=os.environ.get("SMTP_HOST", ""),
    SMTP_PORT=_int("SMTP_PORT", 587),
    SMTP_USERNAME=os.environ.get("SMTP_USERNAME", ""),
    SMTP_PASSWORD=os.environ.get("SMTP_PASSWORD", ""),
    SMTP_FROM_NAME=os.environ.get("SMTP_FROM_NAME", "Payday Tracker"),
    SMTP_FROM_EMAIL=os.environ.get("SMTP_FROM_EMAIL", ""),
    SMTP_USE_TLS=_bool("SMTP_USE_TLS", True),
    BASE_URL=os.environ.get("BASE_URL", "http://127.0.0.1:8000"),
    DEBUG=_bool("DEBUG", False),
    PROJECT_ROOT=_PROJECT_ROOT,
)
