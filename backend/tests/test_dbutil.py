"""
Readers must not create the database they were asked to read.

`sqlite3.connect` creates a missing file. That hid a crash here for as long as
tests ran as root (the "missing" file was silently created, then failed to
query, and the error was swallowed), and on a user's machine it leaves an empty
database behind for every mistyped path. This test fails for any user, root or
not, because it checks the side effect rather than the exception.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_readers_neither_raise_nor_create_a_missing_database():
    from bhavcopy.snapshots import available_dates, iter_eod_snapshots
    from cas import load_cas_history
    from dbutil import open_existing
    from futures import load_basis_history

    with tempfile.TemporaryDirectory() as tmp:
        missing = os.path.join(tmp, "typo.db")
        assert list(iter_eod_snapshots("NIFTY", "2026-09-21", missing)) == []
        assert available_dates("NIFTY", missing) == []
        assert load_basis_history("NIFTY", missing) == []
        assert load_cas_history("NIFTY", missing) == []
        assert open_existing(missing) is None
        assert os.listdir(tmp) == []                  # nothing was created
