# optionslens/backend/alert_engine/db.py
"""
SQLite persistence for the OI Alert Engine.
Stored in oi_engine.db — separate from optionslens.db.

Schema is a direct port of app_v2_final.py with one addition:
  - 'symbol' column on all tables (multi-symbol support)

All functions are synchronous (SQLite ops are fast enough for poll cadence).
"""
import sqlite3
import pandas as pd
from datetime import datetime, timedelta
from alert_engine.models import ALERT_ENGINE_DB


def get_conn(db_path: str = ALERT_ENGINE_DB):
    return sqlite3.connect(db_path, check_same_thread=False)


def init_db(db_path: str = ALERT_ENGINE_DB):
    conn = get_conn(db_path)
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS oi_snapshots (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp      TEXT NOT NULL,
            session_date   TEXT NOT NULL,
            symbol         TEXT NOT NULL DEFAULT 'NIFTY',
            expiry_date    TEXT,
            strike         REAL,
            option_type    TEXT,
            oi             REAL,
            oi_change      REAL,
            oi_change_pct  REAL,
            prev_oi        REAL,
            ltp            REAL,
            volume         REAL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS session_baseline (
            session_date  TEXT NOT NULL,
            symbol        TEXT NOT NULL DEFAULT 'NIFTY',
            strike        REAL NOT NULL,
            option_type   TEXT NOT NULL,
            baseline_oi   REAL,
            baseline_ltp  REAL,
            recorded_at   TEXT,
            PRIMARY KEY (session_date, symbol, strike, option_type)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            triggered_at     TEXT NOT NULL,
            session_date     TEXT NOT NULL,
            symbol           TEXT NOT NULL DEFAULT 'NIFTY',
            expiry_date      TEXT,
            strike           REAL,
            option_type      TEXT,
            signal_direction TEXT,
            trade_strike     REAL,
            trade_option     TEXT,
            oi_pct_change    REAL,
            oi_speed_pct_pm  REAL,
            ltp_at_trigger   REAL,
            ltp_confirmed    REAL,
            premium_behavior TEXT,
            confidence       TEXT,
            suppressed       INTEGER DEFAULT 0,
            suppress_reason  TEXT
        )
    """)

    # Fast lookup index for "today's alerts" queries
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_alerts_session
        ON alerts(session_date, symbol, suppressed)
    """)

    conn.commit()
    conn.close()


def write_snapshots(rows: list, db_path: str = ALERT_ENGINE_DB):
    """Bulk insert snapshot rows. Prune data older than 20 minutes to prevent bloat."""
    if not rows:
        return
    conn = get_conn(db_path)
    c = conn.cursor()
    c.executemany("""
        INSERT INTO oi_snapshots
            (timestamp, session_date, symbol, expiry_date, strike, option_type,
             oi, oi_change, oi_change_pct, prev_oi, ltp, volume)
        VALUES
            (:timestamp, :session_date, :symbol, :expiry_date, :strike, :option_type,
             :oi, :oi_change, :oi_change_pct, :prev_oi, :ltp, :volume)
    """, rows)
    cutoff = (datetime.now() - timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S")
    c.execute("DELETE FROM oi_snapshots WHERE timestamp < ?", (cutoff,))
    conn.commit()
    conn.close()


def write_baseline(session_date: str, symbol: str, strike: float,
                   option_type: str, baseline_oi: float,
                   baseline_ltp: float, recorded_at: str,
                   db_path: str = ALERT_ENGINE_DB):
    conn = get_conn(db_path)
    conn.execute("""
        INSERT OR IGNORE INTO session_baseline
            (session_date, symbol, strike, option_type,
             baseline_oi, baseline_ltp, recorded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (session_date, symbol, strike, option_type,
          baseline_oi, baseline_ltp, recorded_at))
    conn.commit()
    conn.close()


def get_baseline(session_date: str, symbol: str, strike: float,
                 option_type: str, db_path: str = ALERT_ENGINE_DB):
    conn = get_conn(db_path)
    c = conn.cursor()
    c.execute("""
        SELECT baseline_oi, baseline_ltp FROM session_baseline
        WHERE session_date=? AND symbol=? AND strike=? AND option_type=?
    """, (session_date, symbol, strike, option_type))
    row = c.fetchone()
    conn.close()
    return row   # (baseline_oi, baseline_ltp) or None


def get_recent_snapshots(symbol: str, strike: float, option_type: str,
                         minutes: int = 5,
                         db_path: str = ALERT_ENGINE_DB) -> pd.DataFrame:
    conn = get_conn(db_path)
    cutoff = (datetime.now() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    df = pd.read_sql_query("""
        SELECT timestamp, oi, ltp, volume FROM oi_snapshots
        WHERE symbol=? AND strike=? AND option_type=? AND timestamp >= ?
        ORDER BY timestamp ASC
    """, conn, params=(symbol, strike, option_type, cutoff))
    conn.close()
    if not df.empty:
        df['timestamp'] = pd.to_datetime(df['timestamp'])
    return df


def write_alert(alert: dict, db_path: str = ALERT_ENGINE_DB):
    conn = get_conn(db_path)
    conn.execute("""
        INSERT INTO alerts
            (triggered_at, session_date, symbol, expiry_date, strike, option_type,
             signal_direction, trade_strike, trade_option,
             oi_pct_change, oi_speed_pct_pm, ltp_at_trigger, ltp_confirmed,
             premium_behavior, confidence, suppressed, suppress_reason)
        VALUES
            (:triggered_at, :session_date, :symbol, :expiry_date, :strike, :option_type,
             :signal_direction, :trade_strike, :trade_option,
             :oi_pct_change, :oi_speed_pct_pm, :ltp_at_trigger, :ltp_confirmed,
             :premium_behavior, :confidence, :suppressed, :suppress_reason)
    """, alert)
    conn.commit()
    conn.close()


def already_alerted_today(session_date: str, symbol: str, strike: float,
                           option_type: str,
                           db_path: str = ALERT_ENGINE_DB) -> bool:
    conn = get_conn(db_path)
    c = conn.cursor()
    c.execute("""
        SELECT COUNT(*) FROM alerts
        WHERE session_date=? AND symbol=? AND strike=? AND option_type=?
        AND suppressed=0
    """, (session_date, symbol, strike, option_type))
    count = c.fetchone()[0]
    conn.close()
    return count > 0


def load_alerts_today(session_date: str,
                      db_path: str = ALERT_ENGINE_DB) -> list:
    """Return all unsuppressed confirmed alerts for today, newest first."""
    conn = get_conn(db_path)
    df = pd.read_sql_query("""
        SELECT id, triggered_at, symbol, strike, option_type,
               signal_direction, trade_strike, trade_option,
               oi_pct_change, oi_speed_pct_pm,
               ltp_at_trigger, ltp_confirmed,
               premium_behavior, confidence
        FROM alerts
        WHERE session_date=? AND suppressed=0
        ORDER BY triggered_at DESC
    """, conn, params=(session_date,))
    conn.close()
    return df.to_dict(orient='records')


def suppress_alert(alert_id: int, db_path: str = ALERT_ENGINE_DB):
    conn = get_conn(db_path)
    conn.execute("""
        UPDATE alerts
        SET suppressed=1, suppress_reason='USER_SUPPRESSED'
        WHERE id=?
    """, (alert_id,))
    conn.commit()
    conn.close()
