# optionslens/backend/daily_signals.py
"""
The daily signal runner: evaluate every daily signal once per session, log it,
and send a digest (roadmap item 52, signals half).

Until this existed, signals only ever ran inside a backtest. Nothing evaluated
them on the session that just closed, so the system could say what WOULD have
fired in 2019 but not what fired today.

    python -m daily_signals                         # today, the recorder's bars
    python -m daily_signals --source eod            # latest bhavcopy session
    python -m daily_signals --source eod --notify   # ...and send the digest

THE SAME CODE PATH AS THE BACKTESTER, ON PURPOSE
    Bars come from `backtest.replay.replay_session` and history from
    `backtest.extras`, exactly what `run_backtest` uses. A signal's live answer
    for a session is therefore the answer a backtest over that session would
    have logged. If the two could differ, forward results would drift from
    backtest results for reasons unrelated to the signal, and neither could be
    used to check the other.

WHICH SIGNALS
    The daily ones: every registered signal whose module declares an
    `EXTRAS_KEY` (it ranks a daily series), at its highest version and with
    its registered default parameters. Those are the parameters the backtests
    and the pre-registered out-of-sample test judge, so they are the only ones
    worth alerting on. Discovered from the registry, so a new daily signal
    joins the digest without being listed here. The intraday signals
    (gex_regime, oi_buildup) need an intraday runner and are not run.

TWO SOURCES, BECAUSE THE SIGNALS NEED DIFFERENT BARS
    recorder  today's minute bars from the live recorder (`market_data.db`).
              The scheduler runs this at 16:05 IST, after the 15:50 official
              close, so the realised-vol leg includes today. `cas_dislocation`
              can only run here: it reads the auction off intraday bars.
    eod       the exchange's end-of-day bar (`eod_snapshots.db`), built from
              the downloaded bhavcopy. `skew_rr25` can only run here: it
              ranks closing readings and refuses an intraday bar.
    Each source says plainly which signals it could not run and why.

ONE EVALUATION PER SIGNAL PER SESSION
    The whole session is replayed, then one result is kept: the LAST bar that
    fired if any did, otherwise the last bar. A series-ranked signal gives the
    same answer on every bar of a session, so that is its closing reading; the
    auction signal fires on one bar only, so that is the bar it fired on.

NOTHING IS OVERWRITTEN
    Evaluations go to the same log the backtester writes (`signal_evaluations`,
    run id `daily:<source>:<date>`) with INSERT OR IGNORE, so the first
    evaluation of a session is kept: it is what the system knew at the time.
    A session with no bar at all is reported in the digest but not logged as
    an evaluation, since nothing was evaluated.

A DIGEST EVERY SESSION, INCLUDING QUIET ONES
    Silence would be ambiguous: no setup today, or the runner did not run, or
    the data never arrived. The digest separates the three: FIRED, QUIET (the
    signal had today's reading and it was not extreme), UNAVAILABLE (no reading
    to judge). It is sent once per session and source; a rerun does not send
    it again unless forced.

EVERY ALERT SAYS WHETHER ITS SIGNAL HAS EARNED ONE
    `VALIDATED` lists signals that passed the pre-registered out-of-sample test
    (docs/ROADMAP.md). Today it is empty, and every fire is labelled an
    unvalidated hypothesis. A phone alert that looks like advice will be read
    as advice.
"""
import argparse
import asyncio
import importlib
import logging
import os
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator, Optional

import config
from signals.base import SignalResult

logger = logging.getLogger(__name__)

SOURCES = ("recorder", "eod")
DEFAULT_EOD_SNAPSHOT_DB = "eod_snapshots.db"

# Signal ids that passed the pre-registered out-of-sample test. Add one only
# with the run that earned it recorded in docs/ROADMAP.md.
VALIDATED: frozenset[str] = frozenset()

FIRED, QUIET, UNAVAILABLE = "FIRED", "QUIET", "UNAVAILABLE"

# Mirrors run_backtest's default, so the auction signal sees the same window.
HISTORY_WINDOW = 60

REASON_CHARS = 110


@dataclass
class Evaluation:
    """One signal on one symbol for one session."""
    symbol:    str
    signal_id: str
    version:   int
    status:    str
    reason:    str = ""
    result:    Optional[SignalResult] = None

    @property
    def direction(self) -> Optional[str]:
        r = self.result
        return r.signal.direction.value if r and r.signal else None

    @property
    def strength(self) -> Optional[float]:
        r = self.result
        return r.signal.strength if r and r.signal else None

    @property
    def notes(self) -> str:
        r = self.result
        return (r.signal.notes or "") if r and r.signal else ""

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol, "signal_id": self.signal_id,
            "version": self.version, "status": self.status,
            "reason": self.reason, "direction": self.direction,
            "strength": self.strength, "notes": self.notes,
            "ts": self.result.ts if self.result else None,
            "features": self.result.features if self.result else {},
            "validated": self.signal_id in VALIDATED,
        }


@dataclass
class DailyReport:
    session_date: str
    source:       str
    ran_at:       str
    evaluations:  list[Evaluation] = field(default_factory=list)
    # Per-symbol notes from the extras builder: what history was found.
    notes:        dict[str, list[str]] = field(default_factory=dict)

    def having(self, status: str) -> list[Evaluation]:
        return [e for e in self.evaluations if e.status == status]

    def to_dict(self) -> dict:
        return {
            "session_date": self.session_date, "source": self.source,
            "ran_at": self.ran_at,
            "counts": {s: len(self.having(s)) for s in (FIRED, QUIET, UNAVAILABLE)},
            "evaluations": [e.to_dict() for e in self.evaluations],
            "notes": self.notes,
        }


# ── Which signals ─────────────────────────────────────────────────────────────

def daily_signal_specs() -> list:
    """Highest version of every registered series-backed signal."""
    import signals.library  # noqa: F401 — populate the registry
    from signals.registry import list_signals

    latest: dict = {}
    for spec in list_signals():
        module = importlib.import_module(spec.fn.__module__)
        if not hasattr(module, "EXTRAS_KEY"):
            continue
        if spec.signal_id not in latest or spec.version > latest[spec.signal_id].version:
            latest[spec.signal_id] = spec
    return [latest[k] for k in sorted(latest)]


# ── Choosing the session's one result ─────────────────────────────────────────

def choose_result(results: list[SignalResult]) -> Optional[SignalResult]:
    """The last bar that fired, else the last bar, else None."""
    for r in reversed(results):
        if r.fired:
            return r
    return results[-1] if results else None


def classify(result: SignalResult) -> str:
    """
    FIRED, QUIET or UNAVAILABLE.

    A non-fire that carries features got as far as today's reading and judged
    it; one without features stopped before it had anything to judge (no
    series, no reading for the date, the wrong kind of bar).
    """
    if result.fired:
        return FIRED
    return QUIET if result.features else UNAVAILABLE


# ── The run ───────────────────────────────────────────────────────────────────

def default_snapshot_db(source: str) -> str:
    return config.MARKET_DATA_DB if source == "recorder" else DEFAULT_EOD_SNAPSHOT_DB


def latest_eod_date(symbols: list[str], archive_db: Optional[str] = None) -> Optional[str]:
    """The newest session the bhavcopy archive holds for any of `symbols`."""
    from bhavcopy.snapshots import available_dates

    last = [ds[-1] for ds in (available_dates(s, archive_db or config.NSE_EOD_DB)
                              for s in symbols) if ds]
    return max(last) if last else None


def _ensure_eod_bar(symbol: str, session_date: str, snapshot_db: str,
                    archive_db: str) -> None:
    """Materialise this one session from the archive if it is not there yet."""
    from bhavcopy.snapshots import materialize
    from recorder.store import init_db, recorded_dates

    init_db(snapshot_db)
    if session_date not in recorded_dates(symbol, db_path=snapshot_db):
        materialize(symbol, snapshot_db, dates=[session_date], src_db=archive_db,
                    nearest_expiry_only=True)


def run_daily(session_date: str, source: str = "recorder",
              symbols: Optional[list[str]] = None,
              app_db: Optional[str] = None,
              snapshot_db: Optional[str] = None,
              eval_db: Optional[str] = None,
              archive_db: Optional[str] = None,
              persist: bool = True) -> DailyReport:
    """
    Evaluate every daily signal on every symbol for one session.

    Never raises for a missing store or series: each becomes an UNAVAILABLE
    row whose reason says what was missing, because a runner that crashes on
    the first absent file reports nothing about the symbols that were fine.
    """
    from backtest.extras import build_extras_for
    from backtest.replay import replay_session
    from market_hours import now_ist

    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}, not {source!r}")

    symbols = symbols or list(config.UNDERLYINGS)
    app_db = app_db or config.DB_PATH
    snapshot_db = snapshot_db or default_snapshot_db(source)
    eval_db = eval_db or config.MARKET_DATA_DB
    archive_db = archive_db or config.NSE_EOD_DB

    specs = daily_signal_specs()
    report = DailyReport(session_date, source, now_ist().isoformat(timespec="seconds"))
    max_lookback = max((int(s.default_params.get("lookback", 0)) for s in specs),
                       default=0) or None

    for symbol in symbols:
        have_store = True
        if source == "eod":
            try:
                _ensure_eod_bar(symbol, session_date, snapshot_db, archive_db)
            except Exception as e:               # noqa: BLE001
                logger.warning(f"EOD bar for {symbol} {session_date} failed: {e!r}")
        elif not os.path.exists(snapshot_db):
            have_store = False

        if have_store:
            extras, notes = build_extras_for(
                symbol, [s.signal_id for s in specs], app_db=app_db,
                snapshot_db=snapshot_db, until=session_date,
                max_prior_readings=max_lookback)
        else:
            # Nothing can be evaluated, and the skew and CAS loaders would
            # create the missing file just by reading it.
            extras, notes = {}, [f"No {source} store at {snapshot_db}."]
        report.notes[symbol] = notes

        for spec in specs:
            report.evaluations.append(_evaluate_one(
                spec, symbol, session_date, source, snapshot_db, extras,
                have_store, replay_session))

    if persist:
        _persist(report, specs, eval_db)
    return report


def _evaluate_one(spec, symbol, session_date, source, snapshot_db, extras,
                  have_store, replay_session) -> Evaluation:
    base = dict(symbol=symbol, signal_id=spec.signal_id, version=spec.version)
    if not have_store:
        return Evaluation(**base, status=UNAVAILABLE,
                          reason=f"no {source} store at {snapshot_db}")
    try:
        replay = replay_session(spec, symbol, session_date,
                                history_window=HISTORY_WINDOW, extras=extras,
                                db_path=snapshot_db)
    except Exception as e:                       # noqa: BLE001
        logger.error(f"{spec.key} on {symbol} {session_date} raised: {e!r}")
        return Evaluation(**base, status=UNAVAILABLE, reason=f"evaluation failed: {e}")

    result = choose_result(replay.results)
    if result is None:
        hint = ("is the recorder running?" if source == "recorder" else
                "has the bhavcopy download reached this date?")
        return Evaluation(**base, status=UNAVAILABLE,
                          reason=f"no {source} bar for {session_date} — {hint}")
    return Evaluation(**base, status=classify(result),
                      reason=result.reason or "", result=result)


# ── Persistence: the evaluation log and the run record ───────────────────────

def run_id_for(session_date: str, source: str) -> str:
    return f"daily:{source}:{session_date}"


@contextmanager
def _connect(db_path: str) -> Iterator[sqlite3.Connection]:
    """Commit and CLOSE: `with sqlite3.connect()` only commits, and an open
    handle keeps the file locked on Windows."""
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_run_store(db_path: str) -> None:
    with _connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_signal_runs (
                session_date TEXT NOT NULL,
                source       TEXT NOT NULL,
                first_run_at TEXT NOT NULL,
                last_run_at  TEXT NOT NULL,
                fired        INTEGER NOT NULL,
                quiet        INTEGER NOT NULL,
                unavailable  INTEGER NOT NULL,
                notified_at  TEXT,
                PRIMARY KEY (session_date, source)
            )
        """)


def _persist(report: DailyReport, specs: list, eval_db: str) -> None:
    from signals.store import init_db, write_evaluation

    init_db(eval_db)
    params = {s.signal_id: s.default_params for s in specs}
    run_id = run_id_for(report.session_date, report.source)
    for e in report.evaluations:
        if e.result is not None:
            write_evaluation(e.result, run_id=run_id,
                             params=params.get(e.signal_id), db_path=eval_db)

    init_run_store(eval_db)
    counts = [len(report.having(s)) for s in (FIRED, QUIET, UNAVAILABLE)]
    with _connect(eval_db) as conn:
        conn.execute("""
            INSERT INTO daily_signal_runs
                (session_date, source, first_run_at, last_run_at,
                 fired, quiet, unavailable)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_date, source) DO UPDATE SET
                last_run_at = excluded.last_run_at,
                fired = excluded.fired, quiet = excluded.quiet,
                unavailable = excluded.unavailable
        """, (report.session_date, report.source, report.ran_at, report.ran_at,
              *counts))


def notified_at(session_date: str, source: str, db_path: str) -> Optional[str]:
    if not os.path.exists(db_path):
        return None
    init_run_store(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT notified_at FROM daily_signal_runs "
            "WHERE session_date = ? AND source = ?",
            (session_date, source)).fetchone()
    return row[0] if row else None


def _mark_notified(session_date: str, source: str, db_path: str) -> None:
    from market_hours import now_ist

    init_run_store(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE daily_signal_runs SET notified_at = ? "
            "WHERE session_date = ? AND source = ?",
            (now_ist().isoformat(timespec="seconds"), session_date, source))


# ── The digest ────────────────────────────────────────────────────────────────

def _short(text: str, n: int = REASON_CHARS) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


def format_digest(report: DailyReport) -> tuple[str, str]:
    """(title, body) in plain text; channels add their own formatting."""
    fired = report.having(FIRED)
    quiet = report.having(QUIET)
    missing = report.having(UNAVAILABLE)

    title = (f"Signals · {report.session_date} · "
             f"{len(fired)} fired" if fired else
             f"Signals · {report.session_date} · nothing fired")
    lines = [f"Source: {report.source} bars. "
             f"{len(fired)} fired, {len(quiet)} quiet, {len(missing)} unavailable."]

    if fired:
        lines.append("\nFIRED")
        for e in fired:
            tag = "validated" if e.signal_id in VALIDATED else "unvalidated"
            lines.append(f"{e.symbol} · {e.signal_id} v{e.version} → {e.direction} "
                         f"(strength {e.strength:.2f}) [{tag}]")
            if e.notes:
                lines.append(f"  {_short(e.notes, 220)}")

    if quiet:
        lines.append("\nQUIET")
        for e in quiet:
            lines.append(f"{e.symbol} {e.signal_id}: {_short(e.reason)}")

    if missing:
        lines.append("\nUNAVAILABLE")
        # A symbol whose every signal failed for one reason (no bar today) is
        # one line, not one per signal.
        whole: dict[str, list[str]] = {}
        for symbol in dict.fromkeys(e.symbol for e in report.evaluations):
            own = [e for e in report.evaluations if e.symbol == symbol]
            reasons = {e.reason for e in own}
            if all(e.status == UNAVAILABLE for e in own) and len(reasons) == 1:
                whole.setdefault(_short(reasons.pop()), []).append(symbol)
        for reason, syms in whole.items():
            lines.append(f"{', '.join(syms)} (all signals): {reason}")

        covered = {s for syms in whole.values() for s in syms}
        groups: dict[tuple[str, str], list[str]] = {}
        for e in missing:
            if e.symbol not in covered:
                groups.setdefault((e.signal_id, _short(e.reason)), []).append(e.symbol)
        for (signal_id, reason), syms in groups.items():
            lines.append(f"{signal_id} ({', '.join(syms)}): {reason}")

    if VALIDATED:
        lines.append(f"\nValidated out of sample: {', '.join(sorted(VALIDATED))}. "
                     f"Everything else is a hypothesis under test.")
    else:
        lines.append("\nNo signal has passed the out-of-sample test yet. "
                     "These are hypotheses under test, not trade advice.")
    return title, "\n".join(lines)


def digest_notification(report: DailyReport):
    from notify.events import signal_digest

    title, body = format_digest(report)
    return signal_digest(title, body, report.session_date, report.source,
                         any_fired=bool(report.having(FIRED)))


async def notify_report(report: DailyReport, eval_db: Optional[str] = None,
                        force: bool = False, dispatcher=None) -> dict:
    """
    Send the digest once per session and source.

    Marked as sent only when a channel actually delivered it, so an
    unconfigured or failing channel leaves the next run free to try again.
    """
    from notify import get_dispatcher

    eval_db = eval_db or config.MARKET_DATA_DB
    sent_at = notified_at(report.session_date, report.source, eval_db)
    if sent_at and not force:
        return {"sent": False, "reason": f"already sent at {sent_at}"}

    dispatcher = dispatcher or get_dispatcher()
    delivered = await dispatcher.send(digest_notification(report))
    if any(delivered.values()):
        _mark_notified(report.session_date, report.source, eval_db)
        return {"sent": True, "channels": delivered}
    if not delivered:
        if not dispatcher.active_channels:
            return {"sent": False,
                    "reason": "no notification channel is configured "
                              "(set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)"}
        return {"sent": False,
                "reason": "suppressed by the dispatcher (its severity floor, or a "
                          "repeat inside its dedupe window)"}
    return {"sent": False, "reason": "every channel failed", "channels": delivered}


# ── Command line ──────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    from market_hours import now_ist

    p = argparse.ArgumentParser(
        prog="python -m daily_signals",
        description="Evaluate every daily signal for one session and print the digest.")
    p.add_argument("--source", choices=SOURCES, default="recorder",
                   help="recorder: the live recorder's bars (default). "
                        "eod: the downloaded bhavcopy's end-of-day bar.")
    p.add_argument("--date", help="YYYY-MM-DD. Default: today (recorder) or the "
                                  "newest session in the archive (eod).")
    p.add_argument("--symbols", help="Comma-separated. Default: every configured underlying.")
    p.add_argument("--snapshot-db", help="Bars to evaluate. Default: market_data.db "
                                         "(recorder) or eod_snapshots.db (eod).")
    p.add_argument("--notify", action="store_true",
                   help="Send the digest (once per session and source).")
    p.add_argument("--force", action="store_true",
                   help="With --notify, send even if this session was already sent.")
    p.add_argument("--verbose", action="store_true",
                   help="Also print what history each symbol had.")
    args = p.parse_args(argv)

    symbols = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
               if args.symbols else list(config.UNDERLYINGS))

    session_date = args.date
    if session_date is None:
        if args.source == "eod":
            session_date = latest_eod_date(symbols)
            if session_date is None:
                print(f"The archive {config.NSE_EOD_DB} holds no sessions for "
                      f"{', '.join(symbols)}. Run the bhavcopy download first.")
                return 1
        else:
            session_date = now_ist().date().isoformat()
    try:
        datetime.strptime(session_date, "%Y-%m-%d")
    except ValueError:
        print(f"--date must be YYYY-MM-DD, not {session_date!r}")
        return 2

    report = run_daily(session_date, args.source, symbols,
                       snapshot_db=args.snapshot_db)
    title, body = format_digest(report)
    print(title)
    print("=" * len(title))
    print(body)

    if args.verbose:
        print("\nHISTORY")
        for symbol, notes in report.notes.items():
            for n in notes:
                print(f"  {symbol}: {n}")

    if args.notify:
        outcome = asyncio.run(notify_report(report, force=args.force))
        print(f"\nDigest {'sent' if outcome['sent'] else 'not sent'}"
              f"{': ' + outcome['reason'] if outcome.get('reason') else ''}.")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    sys.exit(main())
