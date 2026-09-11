"""
Trade lifecycle tests.

The properties that matter most here are the ones that protect capital:
  - sizing floors to whole lots and never quietly exceeds the risk budget
  - short options are not sized off premium received
  - illegal state transitions raise rather than silently correcting
  - a position that cannot be marked is not exited on a guessed P&L
  - paper fills cross the spread, so forward results match backtest results
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_hours import IST  # noqa: E402
from trading.broker import LiveBroker, PaperBroker, get_broker  # noqa: E402
from trading.entry import EntryRules, check_entry, limit_price  # noqa: E402
from trading.exits import ExitRules, evaluate_exits  # noqa: E402
from trading.manager import TradeManager  # noqa: E402
from trading.models import (  # noqa: E402
    ExitReason, InvalidTransition, Trade, TradeLeg, TradeState,
)
from trading.portfolio import portfolio_risk, mark_trade  # noqa: E402
from trading.sizing import RiskConfig, size_position  # noqa: E402
from trading.store import (  # noqa: E402
    get_trade, init_db, journal_stats, list_trades, save_trade,
)

LOT = 75


def temp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    return path


def make_leg(action="BUY", strike=24500.0, lots=1, opt="CE") -> TradeLeg:
    return TradeLeg(symbol="NIFTY", expiry_date="17-09-2026", strike=strike,
                    option_type=opt, action=action, lots=lots, lot_size=LOT)


def make_trade(state=TradeState.SIGNAL, **kw) -> Trade:
    return Trade(trade_id="T1", state=state, symbol="NIFTY",
                 legs=[make_leg()], created_at="2026-09-11T10:00:00+05:30", **kw)


def chain_row(strike=24500.0, opt="CE", bid=100.0, ask=102.0, ltp=101.0,
              oi=5000.0, volume=2000.0) -> dict:
    return {"strike": strike, "option_type": opt, "bid": bid, "ask": ask,
            "ltp": ltp, "oi": oi, "volume": volume}


# ── Sizing ────────────────────────────────────────────────────────────────────

def test_sizing_floors_to_whole_lots():
    """Rounding up would silently exceed the risk budget on every trade."""
    cfg = RiskConfig(capital=100_000, risk_per_trade_pct=1.0)   # 1,000 budget
    # premium 100 x 75 x 40% stop = 3,000 risk per lot -> 0 lots affordable
    r = size_position(100.0, LOT, True, cfg, stop_loss_pct=40.0)
    assert r.lots == 0
    assert "fraction of a lot" in r.reason


def test_sizing_never_exceeds_the_budget():
    cfg = RiskConfig(capital=1_000_000, risk_per_trade_pct=1.0)  # 10,000
    r = size_position(50.0, LOT, True, cfg, stop_loss_pct=40.0)  # 1,500/lot
    assert r.lots == 6                                # 6 x 1500 = 9,000 <= 10,000
    assert r.risk_amount <= cfg.risk_per_trade


def test_long_option_risk_is_capped_at_the_premium():
    """A long option cannot lose more than it cost, whatever the stop says."""
    cfg = RiskConfig(capital=10_000_000)
    r = size_position(100.0, LOT, True, cfg, stop_loss_pct=300.0)
    assert r.risk_per_lot == 100.0 * LOT      # capped at 100%, not 300%


def test_short_option_is_not_sized_off_premium_received():
    """Collecting 50 does not mean risking 50 — the loss is open-ended."""
    cfg = RiskConfig(capital=1_000_000, short_stop_multiple=2.0)
    long_r  = size_position(50.0, LOT, True,  cfg, stop_loss_pct=40.0)
    short_r = size_position(50.0, LOT, False, cfg, stop_loss_pct=40.0)

    assert short_r.risk_per_lot > long_r.risk_per_lot
    assert short_r.stop_dependent is True
    assert long_r.stop_dependent is False


def test_daily_budget_blocks_further_trades():
    cfg = RiskConfig(capital=1_000_000, daily_risk_budget_pct=3.0)  # 30,000
    r = size_position(50.0, LOT, True, cfg, risk_already_committed=30_000.0)
    assert r.lots == 0 and "daily risk budget exhausted" in r.reason


def test_position_limit_blocks_further_trades():
    cfg = RiskConfig(max_concurrent_positions=3)
    r = size_position(50.0, LOT, True, cfg, open_positions=3)
    assert r.lots == 0 and "position limit" in r.reason


# ── State machine ─────────────────────────────────────────────────────────────

def test_legal_path_through_the_lifecycle():
    t = make_trade()
    for state in (TradeState.PROPOSED, TradeState.OPEN, TradeState.CLOSED,
                  TradeState.JOURNALED):
        t.transition(state)
    assert t.state is TradeState.JOURNALED
    assert t.is_terminal


def test_illegal_transitions_raise():
    t = make_trade()
    with pytest.raises(InvalidTransition):
        t.transition(TradeState.OPEN)          # cannot skip PROPOSED

    t2 = make_trade(state=TradeState.CLOSED)
    with pytest.raises(InvalidTransition):
        t2.transition(TradeState.OPEN)         # cannot reopen


def test_terminal_states_accept_nothing():
    for state in (TradeState.JOURNALED, TradeState.REJECTED):
        t = make_trade(state=state)
        assert t.is_terminal
        with pytest.raises(InvalidTransition):
            t.transition(TradeState.OPEN)


# ── Entry gates ───────────────────────────────────────────────────────────────

def test_wide_spread_is_rejected():
    rules = EntryRules(max_spread_pct=3.0)
    check = check_entry(bid=100.0, ask=110.0, ltp=105.0, oi=5000, volume=2000,
                        spot_now=24500, spot_at_signal=24500, rules=rules)
    assert not check.passed
    assert any("spread" in r for r in check.reasons)


def test_one_sided_book_is_rejected_immediately():
    check = check_entry(bid=0.0, ask=102.0, ltp=101.0, oi=5000, volume=2000,
                        spot_now=24500, spot_at_signal=24500, rules=EntryRules())
    assert not check.passed
    assert "two-sided" in check.reasons[0]


def test_chasing_a_moved_market_is_rejected():
    rules = EntryRules(max_price_drift_pct=0.30)
    check = check_entry(bid=100.0, ask=101.0, ltp=100.5, oi=5000, volume=2000,
                        spot_now=24600, spot_at_signal=24500, rules=rules)
    assert not check.passed
    assert any("chasing" in r for r in check.reasons)


def test_stale_signal_is_rejected_at_fill_time():
    check = check_entry(bid=100.0, ask=101.0, ltp=100.5, oi=5000, volume=2000,
                        spot_now=24500, spot_at_signal=24500,
                        rules=EntryRules(require_revalidation=True),
                        signal_still_valid=False)
    assert not check.passed


def test_all_failures_are_collected_not_short_circuited():
    check = check_entry(bid=100.0, ask=120.0, ltp=110.0, oi=10, volume=5,
                        spot_now=24500, spot_at_signal=24500, rules=EntryRules())
    assert len(check.reasons) >= 3     # spread + OI + volume


def test_limit_price_sits_inside_the_book():
    rules = EntryRules(limit_offset_ticks=1.0)
    buy = limit_price(100.0, 102.0, True, rules)
    assert 101.0 <= buy <= 102.0
    sell = limit_price(100.0, 102.0, False, rules)
    assert 100.0 <= sell <= 101.0


# ── Exits ─────────────────────────────────────────────────────────────────────

def _open_trade(entry=100.0, iv=0.16, delta=0.30, expiry="17-09-2026") -> Trade:
    leg = make_leg()
    leg.entry_price, leg.entry_iv, leg.entry_delta = entry, iv, delta
    leg.expiry_date = expiry
    t = Trade(trade_id="T1", state=TradeState.OPEN, symbol="NIFTY", legs=[leg],
              created_at="2026-09-11T10:00:00+05:30",
              opened_at="2026-09-11T10:00:00+05:30")
    t.mae = t.mfe = 0.0
    return t


def test_stop_loss_fires():
    t = _open_trade()
    entry_cost = 100.0 * LOT
    d = evaluate_exits(t, -0.45 * entry_cost, ExitRules(stop_loss_pct=40.0),
                       now=datetime(2026, 9, 11, 11, 0, tzinfo=IST))
    assert d.should_exit and d.reason is ExitReason.STOP_LOSS


def test_target_fires():
    t = _open_trade()
    d = evaluate_exits(t, 0.9 * 100.0 * LOT, ExitRules(target_pct=80.0),
                       now=datetime(2026, 9, 11, 11, 0, tzinfo=IST))
    assert d.should_exit and d.reason is ExitReason.TARGET


def test_expiry_flatten_outranks_a_winning_position():
    """Being near target is not a reason to carry gamma into settlement."""
    t = _open_trade(expiry="11-09-2026")
    d = evaluate_exits(t, 0.7 * 100.0 * LOT, ExitRules(target_pct=80.0),
                       now=datetime(2026, 9, 11, 15, 15, tzinfo=IST))
    assert d.should_exit and d.reason is ExitReason.EXPIRY_FLATTEN


def test_iv_crush_exits_a_long_that_is_bleeding_on_vol():
    t = _open_trade(iv=0.20)
    d = evaluate_exits(t, 0.0, ExitRules(iv_crush_exit_pct=25.0),
                       now=datetime(2026, 9, 11, 11, 0, tzinfo=IST),
                       current_iv=0.14)          # -30%
    assert d.should_exit and d.reason is ExitReason.IV_CRUSH


def test_iv_crush_does_not_fire_on_a_short():
    """A credit position benefits from falling IV — exiting would be backwards."""
    leg = make_leg(action="SELL")
    leg.entry_price, leg.entry_iv = 100.0, 0.20
    t = Trade(trade_id="T2", state=TradeState.OPEN, symbol="NIFTY", legs=[leg],
              created_at="x", opened_at="2026-09-11T10:00:00+05:30")
    d = evaluate_exits(t, 0.0, ExitRules(iv_crush_exit_pct=25.0),
                       now=datetime(2026, 9, 11, 11, 0, tzinfo=IST),
                       current_iv=0.14)
    assert not d.should_exit


def test_delta_drift_exits_a_changed_position():
    t = _open_trade(delta=0.30)
    d = evaluate_exits(t, 0.0, ExitRules(delta_drift_band=0.35),
                       now=datetime(2026, 9, 11, 11, 0, tzinfo=IST),
                       current_delta=0.80)
    assert d.should_exit and d.reason is ExitReason.DELTA_DRIFT


def test_time_stop_fires():
    t = _open_trade()
    d = evaluate_exits(t, 0.0, ExitRules(max_holding_minutes=60),
                       now=datetime(2026, 9, 11, 12, 0, tzinfo=IST))
    assert d.should_exit and d.reason is ExitReason.TIME_STOP


def test_quiet_position_is_held():
    t = _open_trade()
    d = evaluate_exits(t, 100.0, ExitRules(max_holding_minutes=600),
                       now=datetime(2026, 9, 11, 10, 30, tzinfo=IST),
                       current_iv=0.16, current_delta=0.31)
    assert not d.should_exit


# ── Broker ────────────────────────────────────────────────────────────────────

def test_paper_fill_crosses_the_spread():
    broker = PaperBroker()
    leg = make_leg()
    buy = broker.place(leg, 100.0, 102.0, 101.0, is_buy=True)
    assert buy.filled and buy.price > 101.0          # worse than mid
    sell = broker.place(leg, 100.0, 102.0, 101.0, is_buy=False)
    assert sell.filled and sell.price < 101.0


def test_paper_fill_refuses_an_empty_book():
    result = PaperBroker().place(make_leg(), 0.0, 0.0, 0.0, is_buy=True)
    assert not result.filled and "fictional" in result.reason


def test_broker_factory_defaults_to_paper():
    assert get_broker().is_live is False
    assert get_broker(live=True, token="x").is_live is True


def test_live_broker_refuses_to_place_orders():
    """Live execution must be a deliberate change, not an inherited default."""
    with pytest.raises(NotImplementedError):
        LiveBroker("token").place(make_leg(), 100.0, 102.0, 101.0, True)

    with pytest.raises(ValueError):
        get_broker(live=True)            # no token


# ── Manager end-to-end ────────────────────────────────────────────────────────

def test_full_lifecycle_paper_trade():
    db = temp_db()
    try:
        mgr = TradeManager(risk=RiskConfig(capital=5_000_000), db_path=db)
        row = chain_row()

        trade = mgr.propose("NIFTY", [make_leg()], row, spot=24500.0,
                            signal_id="gex_regime", signal_version=1,
                            direction="LONG_VOL", strength=0.7)
        assert trade.state is TradeState.PROPOSED and trade.legs[0].lots > 0

        trade = mgr.try_enter(trade, row, spot_now=24500.0,
                              entry_iv=0.16, entry_delta=0.3)
        assert trade.state is TradeState.OPEN
        assert trade.legs[0].entry_price > 0

        # Close into a higher book — should be profitable.
        better = [chain_row(bid=140.0, ask=142.0, ltp=141.0)]
        trade = mgr.close(trade, better, spot=24600.0, reason=ExitReason.TARGET)
        assert trade.state is TradeState.CLOSED
        assert trade.realized_pnl > 0
        assert trade.total_charges > 0

        trade = mgr.journal(trade.trade_id, "Worked as intended.")
        assert trade.state is TradeState.JOURNALED

        reloaded = get_trade(trade.trade_id, db_path=db)
        assert reloaded.state is TradeState.JOURNALED
        assert reloaded.postmortem == "Worked as intended."
    finally:
        os.unlink(db)


def test_rejected_proposal_is_still_persisted():
    """A skipped signal with a recorded reason is evidence; a vanished one is not."""
    db = temp_db()
    try:
        mgr = TradeManager(risk=RiskConfig(capital=10_000), db_path=db)
        trade = mgr.propose("NIFTY", [make_leg()], chain_row(), spot=24500.0)
        assert trade.state is TradeState.REJECTED
        assert trade.notes and "Not sized" in trade.notes
        assert get_trade(trade.trade_id, db_path=db) is not None
    finally:
        os.unlink(db)


def test_entry_rejection_is_persisted_with_reason():
    db = temp_db()
    try:
        mgr = TradeManager(risk=RiskConfig(capital=5_000_000), db_path=db)
        trade = mgr.propose("NIFTY", [make_leg()], chain_row(), spot=24500.0)
        wide = chain_row(bid=100.0, ask=130.0)
        trade = mgr.try_enter(trade, wide, spot_now=24500.0)
        assert trade.state is TradeState.REJECTED
        assert "Entry rejected" in trade.notes
    finally:
        os.unlink(db)


def test_unmarkable_position_is_not_exited_on_a_guess():
    db = temp_db()
    try:
        mgr = TradeManager(risk=RiskConfig(capital=5_000_000), db_path=db)
        trade = mgr.propose("NIFTY", [make_leg()], chain_row(), spot=24500.0)
        trade = mgr.try_enter(trade, chain_row(), spot_now=24500.0)

        # Chain missing the traded strike entirely.
        trade, decision = mgr.update_open_trade(
            trade, [chain_row(strike=99999.0)], spot=24500.0)
        assert not decision.should_exit
        assert trade.state is TradeState.OPEN
    finally:
        os.unlink(db)


def test_mae_mfe_track_the_worst_and_best_marks():
    t = _open_trade()
    t.update_excursions(-500.0, "t1")
    t.update_excursions(1200.0, "t2")
    t.update_excursions(-200.0, "t3")
    assert t.mae == -500.0 and t.mfe == 1200.0
    assert t.mae_at == "t1" and t.mfe_at == "t2"


# ── Journal & portfolio ───────────────────────────────────────────────────────

def test_journal_stats_report_avg_mae_alongside_win_rate():
    db = temp_db()
    try:
        for i, (pnl, mae) in enumerate([(500.0, -200.0), (-300.0, -400.0),
                                        (800.0, -900.0)]):
            leg = make_leg()
            leg.entry_price, leg.exit_price = 100.0, 105.0
            t = Trade(trade_id=f"T{i}", state=TradeState.CLOSED, symbol="NIFTY",
                      legs=[leg], created_at="x", realized_pnl=pnl, mae=mae,
                      mfe=abs(mae), exit_reason=ExitReason.TARGET)
            save_trade(t, db)

        stats = journal_stats(db)
        assert stats["trades"] == 3
        assert stats["wins"] == 2 and stats["losses"] == 1
        assert stats["avg_mae"] is not None
        assert stats["profit_factor"] is not None
    finally:
        os.unlink(db)


def test_portfolio_warns_on_short_vega_and_concentration():
    from trading.portfolio import PositionMark
    trades = [make_trade(state=TradeState.OPEN) for _ in range(2)]
    for i, t in enumerate(trades):
        t.trade_id = f"T{i}"
        t.risk_amount = 50_000.0

    marks = [
        PositionMark(trade_id="T0", symbol="NIFTY", unrealized_pnl=100.0,
                     pnl_pct=1.0, delta=0.5, gamma=-0.001, vega=-10.0, theta=5.0),
        PositionMark(trade_id="T1", symbol="NIFTY", unrealized_pnl=-50.0,
                     pnl_pct=-0.5, delta=0.3, gamma=-0.001, vega=-8.0, theta=4.0),
    ]
    risk = portfolio_risk(trades, marks, capital=500_000)

    assert risk.net_vega < 0
    assert any("short vega" in w for w in risk.warnings)
    assert any("short gamma" in w for w in risk.warnings)
    assert risk.open_positions == 2


def test_unmarkable_positions_are_flagged_in_portfolio_warnings():
    from trading.portfolio import PositionMark
    t = make_trade(state=TradeState.OPEN)
    t.risk_amount = 1000.0
    marks = [PositionMark(trade_id="T1", symbol="NIFTY", unrealized_pnl=None,
                          pnl_pct=None, markable=False)]
    risk = portfolio_risk([t], marks)
    assert any("could not be marked" in w for w in risk.warnings)
