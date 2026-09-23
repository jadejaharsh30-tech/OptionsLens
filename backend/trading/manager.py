# optionslens/backend/trading/manager.py
"""
Trade lifecycle orchestration.

Drives SIGNAL -> PROPOSED -> OPEN -> CLOSED -> JOURNALED, persisting at every
step so a restart never loses a position. This is the only module that mutates
trade state; everything else decides *whether* something should happen and
returns a decision object.

Keeping that split matters: `evaluate_exits` is pure and testable precisely
because it does not also close the trade.
"""
import logging
import uuid
from datetime import datetime
from typing import Optional

from market_hours import IST, time_to_expiry
from trading.broker import Broker, PaperBroker
from trading.entry import EntryRules, check_entry
from trading.exits import ExitDecision, ExitRules, evaluate_exits
from trading.models import (
    ExitReason, InvalidTransition, Trade, TradeLeg, TradeState,
)
from trading.portfolio import build_marks, mark_trade
from trading.sizing import RiskConfig, committed_risk, size_position
from trading.store import get_trade, open_trades, save_trade

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(IST).isoformat()


def _new_id() -> str:
    return f"T{uuid.uuid4().hex[:10]}"


class TradeManager:
    """
    Owns the trade lifecycle.

    Stateless between calls apart from its config — every position lives in
    SQLite, so two processes reading the same DB see the same book.
    """

    def __init__(self, broker: Optional[Broker] = None,
                 risk: Optional[RiskConfig] = None,
                 entry_rules: Optional[EntryRules] = None,
                 exit_rules: Optional[ExitRules] = None,
                 db_path: Optional[str] = None):
        self.broker = broker or PaperBroker()
        self.risk = risk or RiskConfig()
        self.entry_rules = entry_rules or EntryRules()
        self.exit_rules = exit_rules or ExitRules()
        self.db_path = db_path
        self._kw = {"db_path": db_path} if db_path else {}

    # ── SIGNAL -> PROPOSED ───────────────────────────────────────────────────

    def propose(self, symbol: str, legs: list[TradeLeg], chain_row: dict,
                spot: float, signal_id: Optional[str] = None,
                signal_version: Optional[int] = None,
                signal_ts: Optional[str] = None,
                direction: Optional[str] = None,
                strength: Optional[float] = None) -> Trade:
        """
        Size a signal into a concrete proposal.

        `chain_row` is the quote for the primary leg, used for sizing and the
        entry checks. A proposal that fails sizing is still persisted as
        REJECTED — a skipped trade with a recorded reason is evidence; a skipped
        trade that vanishes is not.
        """
        trade = Trade(
            trade_id=_new_id(), state=TradeState.SIGNAL, symbol=symbol,
            legs=legs, created_at=_now(), signal_id=signal_id,
            signal_version=signal_version, signal_ts=signal_ts,
            direction=direction, strength=strength,
            entry_spot=spot, paper=not self.broker.is_live,
        )

        primary = legs[0]
        bid = chain_row.get("bid", 0) or 0
        ask = chain_row.get("ask", 0) or 0
        mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else (chain_row.get("ltp") or 0)

        live = open_trades(**self._kw)
        sizing = size_position(
            premium=mid, lot_size=primary.lot_size, is_buy=primary.is_buy,
            cfg=self.risk, stop_loss_pct=self.exit_rules.stop_loss_pct,
            risk_already_committed=committed_risk(live),
            open_positions=len(live),
        )

        if not sizing.tradeable:
            trade.transition(TradeState.REJECTED)
            trade.notes = f"Not sized: {sizing.reason}"
            save_trade(trade, **self._kw)
            return trade

        for leg in trade.legs:
            leg.lots = sizing.lots
        trade.risk_amount = sizing.risk_amount
        trade.max_loss = sizing.max_loss
        trade.meta["sizing"] = sizing.reason
        trade.meta["stop_dependent"] = sizing.stop_dependent

        trade.transition(TradeState.PROPOSED)
        save_trade(trade, **self._kw)
        return trade

    # ── PROPOSED -> OPEN ─────────────────────────────────────────────────────

    def try_enter(self, trade: Trade, chain_row: dict, spot_now: float,
                  signal_still_valid: Optional[bool] = None,
                  entry_iv: Optional[float] = None,
                  entry_delta: Optional[float] = None) -> Trade:
        """Run entry gates and, if they pass, fill the trade."""
        if trade.state is not TradeState.PROPOSED:
            raise InvalidTransition(
                f"{trade.trade_id} is {trade.state.value}, not PROPOSED")

        bid = chain_row.get("bid", 0) or 0
        ask = chain_row.get("ask", 0) or 0
        ltp = chain_row.get("ltp", 0) or 0

        check = check_entry(
            bid=bid, ask=ask, ltp=ltp,
            oi=chain_row.get("oi", 0) or 0,
            volume=chain_row.get("volume", 0) or 0,
            spot_now=spot_now, spot_at_signal=trade.entry_spot or spot_now,
            rules=self.entry_rules, signal_still_valid=signal_still_valid,
        )

        if not check.passed:
            trade.transition(TradeState.REJECTED)
            trade.notes = "Entry rejected: " + "; ".join(check.reasons)
            save_trade(trade, **self._kw)
            return trade

        filled_any = False
        for leg in trade.legs:
            result = self.broker.place(leg, bid, ask, ltp, leg.is_buy)
            if not result.filled:
                trade.transition(TradeState.REJECTED)
                trade.notes = f"Fill failed: {result.reason}"
                save_trade(trade, **self._kw)
                return trade
            leg.entry_price = result.price
            leg.charges = result.charges
            leg.entry_iv = entry_iv
            leg.entry_delta = entry_delta
            trade.total_charges += result.charges
            filled_any = True

        if not filled_any:
            trade.transition(TradeState.REJECTED)
            trade.notes = "No legs filled"
            save_trade(trade, **self._kw)
            return trade

        trade.transition(TradeState.OPEN)
        trade.opened_at = _now()
        trade.entry_spot = spot_now
        trade.mae = trade.mfe = 0.0
        save_trade(trade, **self._kw)
        logger.info(f"Trade opened: {trade.describe()}")
        return trade

    # ── While OPEN ───────────────────────────────────────────────────────────

    def update_open_trade(self, trade: Trade, chain: list[dict], spot: float,
                          expiry_date: Optional[str] = None,
                          forward: Optional[float] = None) -> tuple[Trade, ExitDecision]:
        """
        Mark, track excursions, and evaluate exits.

        Returns the trade and the exit decision without acting on it, so a
        caller can notify or require confirmation before closing.
        """
        if trade.state is not TradeState.OPEN:
            return trade, ExitDecision.hold()

        exp = expiry_date or (trade.legs[0].expiry_date if trade.legs else None)
        T = time_to_expiry(exp) if exp else 0.0
        mark = mark_trade(trade, chain, spot, forward, T)

        if mark.unrealized_pnl is None:
            # Unmarkable: do not guess a P&L, and do not exit on a guess.
            return trade, ExitDecision.hold()

        trade.update_excursions(mark.unrealized_pnl, _now())
        save_trade(trade, **self._kw)

        decision = evaluate_exits(
            trade=trade, unrealized_pnl=mark.unrealized_pnl,
            rules=self.exit_rules, current_iv=mark.iv, current_delta=mark.delta,
        )
        return trade, decision

    # ── OPEN -> CLOSED ───────────────────────────────────────────────────────

    def close(self, trade: Trade, chain: list[dict], spot: float,
              reason: ExitReason = ExitReason.MANUAL,
              detail: str = "") -> Trade:
        """Flatten the position and realise P&L net of all charges."""
        if trade.state is not TradeState.OPEN:
            raise InvalidTransition(
                f"{trade.trade_id} is {trade.state.value}, not OPEN")

        by_key = {(r["strike"], r["option_type"]): r for r in chain}

        for leg in trade.legs:
            row = by_key.get((leg.strike, leg.option_type))
            if not row:
                logger.warning(f"No quote to close {leg.describe()} — using entry price")
                leg.exit_price = leg.entry_price
                continue

            bid = row.get("bid", 0) or 0
            ask = row.get("ask", 0) or 0
            ltp = row.get("ltp", 0) or 0

            # Closing reverses the opening side.
            result = self.broker.place(leg, bid, ask, ltp, is_buy=not leg.is_buy)
            if result.filled:
                leg.exit_price = result.price
                leg.charges += result.charges
                trade.total_charges += result.charges
            else:
                leg.exit_price = ltp or leg.entry_price

        gross = sum(leg.gross_pnl() or 0.0 for leg in trade.legs)
        trade.realized_pnl = round(gross - trade.total_charges, 2)
        trade.exit_spot = spot
        trade.closed_at = _now()
        trade.exit_reason = reason
        trade.notes = (trade.notes + " | " if trade.notes else "") + detail

        trade.transition(TradeState.CLOSED)
        save_trade(trade, **self._kw)
        logger.info(
            f"Trade closed: {trade.trade_id} {reason.value} "
            f"P&L {trade.realized_pnl:+.2f}"
        )
        return trade

    # ── CLOSED -> JOURNALED ──────────────────────────────────────────────────

    def journal(self, trade_id: str, postmortem: str) -> Trade:
        """Attach a postmortem. The step that turns a trade into a lesson."""
        trade = get_trade(trade_id, **self._kw)
        if trade is None:
            raise KeyError(f"No such trade: {trade_id}")
        trade.transition(TradeState.JOURNALED)
        trade.postmortem = postmortem
        save_trade(trade, **self._kw)
        return trade

    # ── Helpers ──────────────────────────────────────────────────────────────

    def auto_postmortem(self, trade: Trade) -> str:
        """
        A factual draft for the journal entry.

        Focuses on the MAE-versus-outcome relationship, because that is the one
        a discretionary review is worst at seeing: a winner that spent most of
        its life deeply underwater says the stop is nearly too tight, and a
        loser that never went green says the entry was wrong, not the exit.
        """
        parts = [
            f"{trade.symbol} {trade.direction or ''} via "
            f"{trade.signal_id or 'manual'} "
            f"v{trade.signal_version if trade.signal_version else '-'}.",
            f"Exit: {trade.exit_reason.value if trade.exit_reason else 'n/a'}.",
            f"Realised {trade.realized_pnl:+.2f} "
            f"(charges {trade.total_charges:.2f}).",
        ]
        if trade.mae is not None and trade.mfe is not None:
            parts.append(f"MAE {trade.mae:+.0f}, MFE {trade.mfe:+.0f}.")
            if trade.realized_pnl and trade.realized_pnl > 0 and trade.mae < 0:
                if abs(trade.mae) > trade.realized_pnl:
                    parts.append(
                        "Winner went further against us than it finally paid — "
                        "a slightly tighter stop would have turned this into a loss."
                    )
            if trade.realized_pnl and trade.realized_pnl < 0 and trade.mfe > 0:
                parts.append(
                    f"Loser was up {trade.mfe:.0f} at its best — worth asking "
                    f"whether a trail would have banked it."
                )
        return " ".join(parts)
