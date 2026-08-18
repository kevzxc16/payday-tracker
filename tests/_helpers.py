"""
Test helpers.

Provides a `TempDBTestCase` base class that points the app at a fresh
sqlite database in a temp directory before each test, so tests don't
pollute each other.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path


class TempDBTestCase(unittest.TestCase):
    """unittest.TestCase that gives each test a fresh on-disk sqlite DB."""

    def setUp(self) -> None:
        self._tmpdir = Path(tempfile.mkdtemp(prefix="pdt-test-"))
        # Force the config singleton to point at this DB. Settings is frozen
        # at import time, so we mutate the file path on the existing instance
        # via object.__setattr__ which sidesteps frozen=True.
        from app.config import settings
        object.__setattr__(settings, "DB_PATH", str(self._tmpdir / "test.db"))
        object.__setattr__(settings, "SECRET_KEY", "test-key")
        object.__setattr__(settings, "DEBUG", True)

        from app import db
        db._reset_connection()  # ensure new path is picked up
        db.init_db()

    def tearDown(self) -> None:
        from app import db
        db._reset_connection()
        shutil.rmtree(self._tmpdir, ignore_errors=True)
