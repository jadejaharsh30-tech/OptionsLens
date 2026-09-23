# optionslens/backend/trading/store.py
"""
Trade persistence.

Lives in the same SQLite file as the recorded market data so that a journal
entry and the chain that produced it can be joined without crossing databases.

Legs are stored in their own table rather than as JSON: positions are queried
by strike and expiry constantly (marking, exits, portfolio Greeks), and a JSON
blob makes every one of those a full scan.
"""
import json
import sqlite3
from contextlib import contextmanager
from typing import Optional

from config import MARKET_DATA_DB
from trading.models import ExitReason, Trade, TradeLeg, TradeState


@contextmanager
def _conn(db_path: str):
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: str = MARKET_DATA_DB):
    with _conn(db_path) as conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                trade_id       TEXT PRIMARY KEY,
                state          TEXT NOT NULL,
                symbol         TEXT NOT NULL,
                created_at     TEXT NOT NULL,
                signal_id      TEXT,
                signal_version INTEGER,
                signal_ts      TEXT,
                direction      TEXT,
                strength       REAL,
                opened_at      TEXT,
                closed_at      TEXT,
                exit_reason    TEXT,
                risk_amount    REAL,
                max_loss       REAL,
                entry_spot     REAL,
                exit_spot      REAL,
                mae            REAL,
                mfe            REAL,
                mae_at         TEXT,
                mfe_at         TEXT,
                realized_pnl   REAL,
                total_charges  REAL DEFAULT 0,
                paper          INTEGER DEFAULT 1,
                notes          TEXT,
                postmortem     TEXT,
                meta           TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS trade_legs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id     TEXT NOT NULL REFERENCES trades(trade_id) ON DELETE CASCADE,
                leg_index    INTEGER NOT NULL,
                symbol       TEXT NOT NULL,
                expiry_date  TEXT NOT NULL,
                strike       REAL NOT NULL,
                option_type  TEXT NOT NULL,
                action       TEXT NOT NULL,
                lots         INTEGER NOT NULL,
                lot_size     INTEGER NOT NULL,
                entry_price  REAL,
                exit_price   REAL,
                entry_iv     REAL,
                exit_iv      REAL,
                entry_delta  REAL,
                charges      REAL DEFAULT 0,
                UNIQUE(trade_id, leg_index)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_trades_state ON trades(state, symbol)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_trades_signal ON trades(signal_id, created_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_legs_trade ON trade_legs(trade_id)")


def save_trade(trade: Trade, db_path: str = MARKET_DATA_DB):
    """Upsert a trade and replace its legs. Safe to call on every state change."""
    with _conn(db_path) as conn:
        conn.execute("""
            INSERT INTO trades (
                trade_id, state, symbol, created_at, signal_id, signal_version,
                signal_ts, direction, strength, opened_at, closed_at, exit_reason,
                risk_amount, max_loss, entry_spot, exit_spot, mae, mfe, mae_at,
                mfe_at, realized_pnl, total_charges, paper, notes, postmortem, meta
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(trade_id) DO UPDATE SET
                state=excluded.state, opened_at=excluded.opened_at,
                closed_at=excluded.closed_at, exit_reason=excluded.exit_reason,
                risk_amount=excluded.risk_amount, max_loss=excluded.max_loss,
                entry_spot=excluded.entry_spot, exit_spot=excluded.exit_spot,
                mae=excluded.mae, mfe=excluded.mfe, mae_at=excluded.mae_at,
                mfe_at=excluded.mfe_at, realized_pnl=excluded.realized_pnl,
                total_charges=excluded.total_charges, notes=excluded.notes,
                postmortem=excluded.postmortem, meta=excluded.meta
        """, (
            trade.trade_id, trade.state.value, trade.symbol, trade.created_at,
            trade.signal_id, trade.signal_version, trade.signal_ts,
            trade.direction, trade.strength, trade.opened_at, trade.closed_at,
            trade.exit_reason.value if trade.exit_reason else None,
            trade.risk_amount, trade.max_loss, trade.entry_spot, trade.exit_spot,
            trade.mae, trade.mfe, trade.mae_at, trade.mfe_at,
            trade.realized_pnl, trade.total_charges,
            1 if trade.paper else 0, trade.notes, trade.postmortem,
            json.dumps(trade.meta or {}),
        ))

        conn.execute("DELETE FROM trade_legs WHERE trade_id = ?", (trade.trade_id,))
        conn.executemany("""
            INSERT INTO trade_legs (
                trade_id, leg_index, symbol, expiry_date, strike, option_type,
                action, lots, lot_size, entry_price, exit_price, entry_iv,
                exit_iv, entry_delta, charges
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, [
            (trade.trade_id, i, leg.symbol, leg.expiry_date, leg.strike,
             leg.option_type, leg.action, leg.lots, leg.lot_size,
             leg.entry_price, leg.exit_price, leg.entry_iv, leg.exit_iv,
             leg.entry_delta, leg.charges)
            for i, leg in enumerate(trade.legs)
        ])


def _row_to_trade(row: sqlite3.Row, legs: list[sqlite3.Row]) -> Trade:
    return Trade(
        trade_id=row["trade_id"], state=TradeState(row["state"]),
        symbol=row["symbol"], created_at=row["created_at"],
        signal_id=row["signal_id"], signal_version=row["signal_version"],
        signal_ts=row["signal_ts"], direction=row["direction"],
        strength=row["strength"], opened_at=row["opened_at"],
        closed_at=row["closed_at"],
        exit_reason=ExitReason(row["exit_reason"]) if row["exit_reason"] else None,
        risk_amount=row["risk_amount"], max_loss=row["max_loss"],
        entry_spot=row["entry_spot"], exit_spot=row["exit_spot"],
        mae=row["mae"], mfe=row["mfe"], mae_at=row["mae_at"], mfe_at=row["mfe_at"],
        realized_pnl=row["realized_pnl"], total_charges=row["total_charges"] or 0.0,
        paper=bool(row["paper"]), notes=row["notes"], postmortem=row["postmortem"],
        meta=json.loads(row["meta"] or "{}"),
        legs=[
            TradeLeg(
                symbol=l["symbol"], expiry_date=l["expiry_date"], strike=l["strike"],
                option_type=l["option_type"], action=l["action"], lots=l["lots"],
                lot_size=l["lot_size"], entry_price=l["entry_price"],
                exit_price=l["exit_price"], entry_iv=l["entry_iv"],
                exit_iv=l["exit_iv"], entry_delta=l["entry_delta"],
                charges=l["charges"] or 0.0,
            )
            for l in legs
        ],
    )


def get_trade(trade_id: str, db_path: str = MARKET_DATA_DB) -> Optional[Trade]:
    with _conn(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM trades WHERE trade_id = ?",
                           (trade_id,)).fetchone()
        if not row:
            return None
        legs = conn.execute(
            "SELECT * FROM trade_legs WHERE trade_id = ? ORDER BY leg_index",
            (trade_id,)).fetchall()
    return _row_to_trade(row, legs)


def list_trades(state: Optional[TradeState] = None, symbol: Optional[str] = None,
                limit: int = 200, db_path: str = MARKET_DATA_DB) -> list[Trade]:
    clauses, args = [], []
    if state:
        clauses.append("state = ?"); args.append(state.value)
    if symbol:
        clauses.append("symbol = ?"); args.append(symbol)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    with _conn(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT * FROM trades {where} ORDER BY created_at DESC LIMIT ?",
            (*args, limit)).fetchall()
        out = []
        for row in rows:
            legs = conn.execute(
                "SELECT * FROM trade_legs WHERE trade_id = ? ORDER BY leg_index",
                (row["trade_id"],)).fetchall()
            out.append(_row_to_trade(row, legs))
    return out


def open_trades(symbol: Optional[str] = None,
                db_path: str = MARKET_DATA_DB) -> list[Trade]:
    return list_trades(TradeState.OPEN, symbol, db_path=db_path)


def journal_stats(db_path: str = MARKET_DATA_DB) -> dict:
    """
    Closed-trade summary.

    Reports average MAE alongside win rate: if winners routinely sit deeply
    underwater before working, the stop is too tight and the strategy is being
    stopped out of trades that would have paid.
    """
    with _conn(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT realized_pnl, mae, mfe, exit_reason, risk_amount
            FROM trades
            WHERE state IN ('CLOSED','JOURNALED') AND realized_pnl IS NOT NULL
        """).fetchall()

    if not rows:
        return {"trades": 0, "note": "No closed trades yet."}

    pnls = [r["realized_pnl"] for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    maes = [r["mae"] for r in rows if r["mae"] is not None]

    by_reason: dict[str, dict] = {}
    for r in rows:
        key = r["exit_reason"] or "UNKNOWN"
        bucket = by_reason.setdefault(key, {"count": 0, "pnl": 0.0})
        bucket["count"] += 1
        bucket["pnl"] += r["realized_pnl"]

    gross_win = sum(wins)
    gross_loss = abs(sum(losses))

    return {
        "trades":        len(rows),
        "wins":          len(wins),
        "losses":        len(losses),
        "win_rate_pct":  round(len(wins) / len(rows) * 100, 2),
        "total_pnl":     round(sum(pnls), 2),
        "avg_pnl":       round(sum(pnls) / len(rows), 2),
        "avg_win":       round(gross_win / len(wins), 2) if wins else None,
        "avg_loss":      round(-gross_loss / len(losses), 2) if losses else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "expectancy":    round(sum(pnls) / len(rows), 2),
        "avg_mae":       round(sum(maes) / len(maes), 2) if maes else None,
        "best":          round(max(pnls), 2),
        "worst":         round(min(pnls), 2),
        "by_exit_reason": {
            k: {"count": v["count"], "pnl": round(v["pnl"], 2)}
            for k, v in sorted(by_reason.items())
        },
    }
