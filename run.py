"""
Entry point for the Payday Tracker.

Usage:
    python run.py            # start the server
    python run.py --init-db  # only initialize the schema and exit
"""
from __future__ import annotations

import sys

from app.db import init_db
from app.server import serve


def main() -> None:
    init_db()
    if "--init-db" in sys.argv:
        print("Database initialized.")
        return
    serve()


if __name__ == "__main__":
    main()
