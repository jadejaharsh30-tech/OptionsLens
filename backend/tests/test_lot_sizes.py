# optionslens/backend/tests/test_lot_sizes.py
"""Lot sizes resolved per contract from imported exchange data."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import UNDERLYINGS                           # noqa: E402
from lot_sizes import init_db, lot_size_for, record_lot_sizes  # noqa: E402


def test_exact_expiry_wins_during_a_revision(tmp_path):
    db = str(tmp_path / "app.db")
    record_lot_sizes([("NIFTY", "2026-09-29", 75, "2026-06-01", "2026-09-10"),
                      ("NIFTY", "2026-12-29", 65, "2026-09-01", "2026-09-10")], db_path=db)
    assert lot_size_for("NIFTY", "29-09-2026", db_path=db) == 75
    assert lot_size_for("NIFTY", "2026-12-29", db_path=db) == 65


def test_unknown_expiry_takes_the_most_recently_listed_contract(tmp_path):
    db = str(tmp_path / "app.db")
    record_lot_sizes([("NIFTY", "2026-09-29", 75, "2026-06-01", "2026-09-10"),
                      ("NIFTY", "2026-12-29", 65, "2026-09-01", "2026-09-10")], db_path=db)
    assert lot_size_for("NIFTY", "30-03-2027", db_path=db) == 65
    assert lot_size_for("nifty", db_path=db) == 65


def test_falls_back_to_config_when_never_imported(tmp_path):
    db = str(tmp_path / "app.db")
    init_db(db)
    assert lot_size_for("TCS", db_path=db) == UNDERLYINGS["TCS"]["lot_size"]


def test_missing_table_or_file_degrades_to_config(tmp_path):
    assert lot_size_for("NIFTY", db_path=str(tmp_path / "absent.db")) == \
        UNDERLYINGS["NIFTY"]["lot_size"]


def test_unknown_symbol_is_one(tmp_path):
    assert lot_size_for("NOPE", db_path=str(tmp_path / "absent.db")) == 1


def test_reimport_widens_the_seen_range_instead_of_replacing_it(tmp_path):
    db = str(tmp_path / "app.db")
    record_lot_sizes([("NIFTY", "2026-12-29", 65, "2026-09-01", "2026-09-10")], db_path=db)
    record_lot_sizes([("NIFTY", "2026-12-29", 65, "2026-08-20", "2026-09-05")], db_path=db)
    conn = sqlite3.connect(db)
    got = conn.execute("SELECT first_seen, last_seen FROM contract_lot_size").fetchone()
    conn.close()
    assert got == ("2026-08-20", "2026-09-10")
