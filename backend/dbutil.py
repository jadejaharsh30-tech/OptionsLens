# optionslens/backend/dbutil.py
"""
Opening a database that is supposed to exist already.

`sqlite3.connect(path)` never fails on a missing file: it CREATES one. For a
reader that is two bugs in one call. A mistyped archive path silently leaves an
empty database behind, and whether the call raises at all depends on whether
the process may write to that directory — which is how
`iter_eod_snapshots("/nonexistent.db")` returned [] when run as root and
raised on CI as an ordinary user. Readers go through here instead.
"""
import sqlite3
from pathlib import Path
from typing import Optional


def open_existing(path: str) -> Optional[sqlite3.Connection]:
    """A connection to `path` if it exists and opens, else None. Never creates."""
    if not path or not Path(path).is_file():
        return None
    try:
        return sqlite3.connect(path)
    except sqlite3.Error:
        return None
