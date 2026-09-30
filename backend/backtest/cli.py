# optionslens/backend/backtest/cli.py
"""
Run signals over the exchange's end-of-day history from the command line.

    python -m backtest.cli run --signal vrp --symbol NIFTY
    python -m backtest.cli run --signal skew_rr25 --symbol NIFTY --param mode=momentum
    python -m backtest.cli correlation --symbol NIFTY --signals vrp,term_structure,dispersion

WHY THIS EXISTS
    The Research page replays the RECORDER's store, which only holds the
    sessions recorded live. The ~two years of history the bhavcopy import
    produced lives elsewhere: IV and closes in `optionslens.db`, option chains
    in the archive. To replay a signal over that history, the archive's rows
    must first be rebuilt as ChainSnapshots (`bhavcopy.snapshots`) into a
    SEPARATE file, then the backtester pointed at it. This command does both.

    It hands signals exactly the series the API would (`backtest.extras`), so a
    result here and a result on the Research page differ only in the bars
    replayed, never in how the history was computed.

KEEPING IT CURRENT
    Every run compares the archive's dates with the snapshot file's and
    materialises only what is missing, so downloading new days and re-running
    is enough. Dates the archive holds but cannot price (files before
    2024-07-08 publish no underlying) are retried and skipped each time; that
    costs seconds, not correctness.
"""
import argparse
import sys
from typing import Any, Optional

DEFAULT_SNAPSHOT_DB = "eod_snapshots.db"


def ensure_snapshots(symbol: str, snapshot_db: str,
                     archive_db: Optional[str] = None) -> dict:
    """
    Materialise archive dates not yet in `snapshot_db`. Returns counts.

    Nearest expiry only, so each session is exactly one bar: that is what makes
    the backtester carry history across sessions and score on daily horizons.
    """
    import config
    from bhavcopy.snapshots import available_dates, materialize
    from recorder.store import init_db, recorded_dates

    archive_db = archive_db or config.NSE_EOD_DB
    init_db(snapshot_db)
    have = set(recorded_dates(symbol, db_path=snapshot_db))
    missing = [d for d in available_dates(symbol, archive_db) if d not in have]
    stats = {"already": len(have), "attempted": len(missing), "added": 0}
    if missing:
        out = materialize(symbol, snapshot_db, dates=missing, src_db=archive_db,
                          nearest_expiry_only=True)
        stats["added"] = out["dates_written"]
    stats["total"] = len(recorded_dates(symbol, db_path=snapshot_db))
    return stats


def _parse_params(pairs: list[str]) -> dict[str, Any]:
    """key=value pairs, with numbers and booleans converted."""
    out: dict[str, Any] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"--param expects key=value, got {pair!r}")
        k, v = pair.split("=", 1)
        if v.lower() in ("true", "false"):
            out[k] = v.lower() == "true"
            continue
        try:
            out[k] = int(v)
        except ValueError:
            try:
                out[k] = float(v)
            except ValueError:
                out[k] = v
    return out


def _fmt(x, nd=2) -> str:
    return "-" if x is None else f"{x:.{nd}f}"


def print_run(run: dict, notes: list[str]) -> None:
    unit = "vol pts" if run["unit"] == "vol_points" else "bps"
    print(f"\n{run['signal_id']}.v{run['version']} on {run['symbol']} — "
          f"{run['sessions']} sessions, {run['signals_fired']} fired "
          f"({run['fire_rate_pct']}%), {run['label_mode']} labels, unit: {unit}")
    print(f"params: {run['params']}")
    for n in notes:
        print(f"  · {n}")

    if run["horizons"]:
        shift = any(r.get("test") == "circular_shift" for r in run["horizons"].values())
        stat = "p" if shift else "t"
        print(f"\n{'horizon':<8}{'n':>6}{'hit %':>8}{'signal':>10}{'null':>10}"
              f"{'edge':>10}{stat:>9}  verdict")
        for h, row in run["horizons"].items():
            sig, null = row["signal"], row["null"]
            val = row.get("shift_p") if row.get("test") == "circular_shift" \
                else row["edge_t_stat"]
            print(f"{h:<8}{sig['n']:>6}{_fmt(sig['hit_rate_pct'], 1):>8}"
                  f"{_fmt(sig['mean_bps']):>10}{_fmt(null['mean_bps']):>10}"
                  f"{_fmt(row['edge_bps']):>10}{_fmt(val, 3 if shift else 2):>9}  "
                  f"{row['verdict']}")
        print(f"\nsignal / null / edge are mean outcomes in {unit}; the null is "
              f"random entries matched on weekday.")
        if shift:
            print("p is from the circular-shift test (EDGE needs p <= 0.05); it "
                  "accounts for overlapping outcomes and runs of fires.")

    if run.get("by_direction"):
        print(f"\nBy direction — baseline is what that direction earned on an "
              f"average session:")
        print(f"{'direction':<11}{'horizon':<9}{'n':>5}{'mean':>9}{'baseline':>10}"
              f"{'edge':>9}{'p':>8}  verdict")
        for name, d in run["by_direction"].items():
            for h, r in d["horizons"].items():
                print(f"{name:<11}{h:<9}{r['n']:>5}{_fmt(r['mean']):>9}"
                      f"{_fmt(r['baseline']):>10}{_fmt(r['edge']):>9}"
                      f"{_fmt(r['shift_p'], 3):>8}  {r['verdict']}")
    print()
    for n in run["notes"]:
        print(f"  · {n}")


def print_correlation(report: dict) -> None:
    print(f"\n{'signal':<18}{'family':<13}{'sessions':>9}{'fires':>7}{'rate %':>8}")
    for sid, s in sorted(report["signals"].items()):
        print(f"{sid:<18}{s['family'] or '-':<13}{s['sessions']:>9}"
              f"{s['fires']:>7}{s['fire_rate_pct']:>8}")
    print(f"\n{'pair':<36}{'joint':>6}{'lift':>7}{'agree':>7}{'corr':>7}  verdict")
    for p in report["pairs"]:
        pair = f"{p['a']} × {p['b']}"
        print(f"{pair:<36}{p['joint_fires']:>6}{_fmt(p['lift']):>7}"
              f"{_fmt(p['agreement']):>7}{_fmt(p['signed_corr']):>7}  {p['verdict']}")
    for n in report["notes"]:
        print(f"  · {n}")


def main(argv: Optional[list[str]] = None) -> int:
    import config
    import signals.library  # noqa: F401 — populate the registry
    from backtest.correlation import correlation_report, session_activity
    from backtest.engine import run_backtest
    from backtest.extras import build_extras
    from recorder.store import recorded_dates
    from signals.registry import get_signal

    ap = argparse.ArgumentParser(
        description="Backtest signals over the exchange EOD history.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--db", default=DEFAULT_SNAPSHOT_DB,
                    help="materialised EOD snapshots (separate from market_data.db)")
    ap.add_argument("--app-db", default=None, help="IV/spot history (default DB_PATH)")
    ap.add_argument("--archive", default=None, help="bhavcopy archive (default NSE_EOD_DB)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="backtest one signal against its matched null")
    r.add_argument("--signal", required=True)
    r.add_argument("--symbol", default="NIFTY")
    r.add_argument("--param", action="append", default=[],
                   help="override a default parameter, key=value (repeatable)")
    r.add_argument("--seed", type=int, default=42)

    c = sub.add_parser("correlation", help="how often signals fire together")
    c.add_argument("--symbol", default="NIFTY")
    c.add_argument("--signals", default="vrp,term_structure,dispersion",
                   help="comma-separated signal ids")
    for sp in (r, c):
        sp.add_argument("--from", dest="start", default=None,
                        help="first session to replay, YYYY-MM-DD (default: all)")
        sp.add_argument("--to", dest="end", default=None,
                        help="last session to replay, YYYY-MM-DD (default: all). "
                             "Signals still rank against history before --from.")

    args = ap.parse_args(argv)
    app_db = args.app_db or config.DB_PATH

    ids = [args.signal] if args.cmd == "run" else \
        [s.strip() for s in args.signals.split(",") if s.strip()]
    for sid in ids:
        try:
            get_signal(sid)
        except KeyError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2

    stats = ensure_snapshots(args.symbol, args.db, args.archive)
    print(f"EOD snapshots for {args.symbol} in {args.db}: {stats['total']} sessions"
          f" ({stats['added']} added this run).")
    dates = recorded_dates(args.symbol, db_path=args.db)
    if args.start:
        dates = [d for d in dates if d >= args.start]
    if args.end:
        dates = [d for d in dates if d <= args.end]
    if dates and (args.start or args.end):
        print(f"Replaying {len(dates)} sessions, {dates[0]} to {dates[-1]}.")
    if not dates:
        print(f"No EOD history for {args.symbol}. Is the archive populated? "
              f"See docs/BHAVCOPY.md.", file=sys.stderr)
        return 1

    if args.cmd == "run":
        extras, notes = build_extras(args.symbol, args.signal,
                                     app_db=app_db, snapshot_db=args.db)
        run = run_backtest(args.signal, args.symbol, params=_parse_params(args.param),
                           session_dates=dates, db_path=args.db, extras=extras,
                           seed=args.seed, persist_evaluations=False)
        print_run(run.to_dict(), notes)
        return 0

    extras: dict[str, Any] = {}
    notes: list[str] = []
    for sid in ids:
        e, n = build_extras(args.symbol, sid, app_db=app_db, snapshot_db=args.db)
        extras.update(e)
        notes.extend(x for x in n if x not in notes)
    activities = {sid: session_activity(sid, args.symbol, dates, extras=extras,
                                        db_path=args.db)
                  for sid in ids}
    report = correlation_report(activities)
    report["notes"] = notes + report["notes"]
    print_correlation(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
