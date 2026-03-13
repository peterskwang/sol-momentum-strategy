"""
strategy/portfolio.py — Multi-Pair Portfolio Orchestration

Coordinates signals, position sizing, risk aggregation, and execution
across all trading pairs. Enforces portfolio-level risk caps.
"""

import time
import logging
from datetime import datetime, timezone
from typing import Optional, Tuple

from config import (
    SYMBOLS,
    RISK_PCT,
    PAIR_WEIGHTS,
    MAX_PORTFOLIO_RISK_PCT,
    MAX_PORTFOLIO_NOTIONAL_X,
    STOP_ATR_MULT,
)
from strategy.regime_filter import update_regime
from strategy.signal_generator import generate_entry_signal
from strategy.funding_rate import fetch_current_funding_rate, get_funding_boost
from strategy.position_sizer import compute_position_size, get_symbol_lot_size
from strategy.exit_manager import (
    TradeState,
    compute_initial_exits,
    update_trailing_stop,
)

logger = logging.getLogger(__name__)


class Portfolio:
    def __init__(self, client, state: dict, config: dict, order_manager=None, notifier=None):
        """
        Initialize Portfolio.

        Args:
            client: Binance Client instance
            state: strategy_state dict (mutable)
            config: config dict from load_config()
            order_manager: OrderManager instance (or paper mode simulator)
            notifier: optional callable(msg: str) for Telegram alerts
        """
        self.client = client
        self.state = state
        self.config = config
        self.order_manager = order_manager
        self.notifier = notifier
        self._equity_cache: Optional[float] = None
        self._equity_cache_ts: float = 0.0

    def get_account_equity(self) -> float:
        """
        Fetch current USDT available balance from /fapi/v3/balance.
        Returns availableBalance for USDT asset.
        Caches result for 60 seconds.
        """
        now = time.time()
        if self._equity_cache is not None and (now - self._equity_cache_ts) < 60:
            return self._equity_cache

        # Paper mode: return paper equity from state
        if not self.config.get("live_trading"):
            equity = float(self.state.get("paper_equity", self.config.get("paper_initial_equity", 10000.0)))
            self._equity_cache = equity
            self._equity_cache_ts = now
            return equity

        # Live mode: fetch from Binance
        balances = self.client.futures_account_balance()
        for b in balances:
            if b.get("asset") == "USDT":
                equity = float(b.get("availableBalance", 0.0))
                self._equity_cache = equity
                self._equity_cache_ts = now
                return equity

        raise ValueError("USDT balance not found in futures account")

    def get_total_open_risk(self) -> Tuple[float, float]:
        """
        Sum risk dollars across all open positions in state.

        Returns:
            (total_risk_fraction, total_notional)
            risk_fraction: total risk as fraction of equity (e.g. 0.015 = 1.5%)
        """
        equity = self.get_account_equity()
        if equity <= 0:
            return 0.0, 0.0

        total_risk_dollars = 0.0
        total_notional = 0.0

        for sym, pos in self.state.get("open_positions", {}).items():
            entry = float(pos.get("entry_price", 0.0))
            stop = float(pos.get("stop_price", 0.0))
            qty = float(pos.get("quantity_remaining", 0.0))

            if entry > 0 and stop > 0 and qty > 0:
                trade_risk = (entry - stop) * qty
                total_risk_dollars += max(0.0, trade_risk)
                total_notional += qty * entry

        return total_risk_dollars / equity, total_notional

    def can_open_position(
        self,
        symbol: str,
        proposed_risk_dollars: float,
        proposed_notional: float,
    ) -> Tuple[bool, str]:
        """
        Check portfolio-level conditions before opening a new position.

        Returns:
            (allowed: bool, reason: str)

        Checks:
            1. Symbol not already in open positions
            2. Total open risk + proposed_risk / equity <= MAX_PORTFOLIO_RISK_PCT (2%)
            3. Total notional + proposed_notional <= MAX_PORTFOLIO_NOTIONAL_X × equity
        """
        # Check 1: No duplicate positions
        if symbol in self.state.get("open_positions", {}):
            return False, f"Already in position for {symbol}"

        equity = self.get_account_equity()
        total_risk_frac, total_notional = self.get_total_open_risk()

        # Check 2: Portfolio risk cap
        proposed_risk_frac = proposed_risk_dollars / equity if equity > 0 else 0
        combined_risk_frac = total_risk_frac + proposed_risk_frac

        if combined_risk_frac > MAX_PORTFOLIO_RISK_PCT:
            return False, (
                f"Portfolio risk cap: current={total_risk_frac:.2%} + "
                f"proposed={proposed_risk_frac:.2%} = {combined_risk_frac:.2%} "
                f"> {MAX_PORTFOLIO_RISK_PCT:.2%}"
            )

        # Check 3: Notional exposure cap
        combined_notional = total_notional + proposed_notional
        max_notional = MAX_PORTFOLIO_NOTIONAL_X * equity

        if combined_notional > max_notional:
            return False, (
                f"Notional cap: {combined_notional:.2f} > {max_notional:.2f} "
                f"({MAX_PORTFOLIO_NOTIONAL_X}× equity)"
            )

        return True, "OK"

    def run_signal_cycle(self) -> list:
        """
        Called on every 4H candle close.

        For each symbol in SYMBOLS:
            1. Check regime (from cached state)
            2. Generate entry signal
            3. If signal: compute position size, check portfolio cap, place order

        Returns:
            List of action dicts (for logging and testing)

        Processing order: SOLUSDT → ETHUSDT → AVAXUSDT
        """
        actions = []
        regime = self.state.get("btc_regime", "BEAR")

        logger.info("=== 4H Signal Cycle | Regime: %s ===", regime)

        for symbol in SYMBOLS:
            try:
                action = self._process_symbol(symbol, regime)
                actions.append(action)
            except Exception as exc:
                logger.error("[%s] Unexpected error in signal cycle: %s", symbol, exc, exc_info=True)
                actions.append({"symbol": symbol, "action": "error", "error": str(exc)})

        return actions

    def _process_symbol(self, symbol: str, regime: str) -> dict:
        """Process a single symbol in the signal cycle."""
        # Check for existing position first
        if symbol in self.state.get("open_positions", {}):
            logger.info("[%s] Already in position — skipping signal check", symbol)
            return {"symbol": symbol, "action": "skipped", "reason": "existing_position"}

        # Generate entry signal
        signal = generate_entry_signal(self.client, symbol, regime)

        if not signal["signal"]:
            return {"symbol": symbol, "action": "no_signal", "reason": signal["reason"]}

        # Fetch funding rate and compute boost
        try:
            funding_rate = fetch_current_funding_rate(self.client, symbol)
            boost = get_funding_boost(funding_rate)
        except Exception as exc:
            logger.warning("[%s] Failed to fetch funding rate: %s — using boost=1.0", symbol, exc)
            funding_rate = 0.0
            boost = 1.0

        # Get account equity and lot size
        equity = self.get_account_equity()

        try:
            lot_size = get_symbol_lot_size(self.client, symbol)
        except Exception as exc:
            logger.warning("[%s] Failed to get lot size: %s — skipping", symbol, exc)
            return {"symbol": symbol, "action": "skipped", "reason": f"lot_size_error: {exc}"}

        # Compute position size
        size = compute_position_size(
            account_equity=equity,
            symbol=symbol,
            atr14=signal["atr"],
            current_price=signal["close"],
            funding_boost=boost,
            lot_size=lot_size,
        )

        if size["quantity"] <= 0:
            return {"symbol": symbol, "action": "skipped", "reason": "quantity_below_min"}

        # Check portfolio caps
        allowed, cap_reason = self.can_open_position(
            symbol=symbol,
            proposed_risk_dollars=size["risk_dollars"],
            proposed_notional=size["notional"],
        )

        if not allowed:
            logger.info("[%s] Portfolio cap rejected: %s", symbol, cap_reason)
            return {"symbol": symbol, "action": "rejected", "reason": cap_reason}

        # Place entry order
        self._open_position(symbol, signal, size, funding_rate, boost)

        return {
            "symbol": symbol,
            "action": "opened",
            "entry_price": signal["close"],
            "quantity": size["quantity"],
            "stop_loss": size["stop_loss"],
            "tp1_price": size["tp1_price"],
        }

    def _open_position(
        self,
        symbol: str,
        signal: dict,
        size: dict,
        funding_rate: float,
        boost: float,
    ) -> None:
        """Execute entry order and record position in state."""
        from execution.order_manager import place_market_order, place_stop_loss_order, place_limit_tp_order
        from state_manager import save_state

        # Place market buy
        entry_resp = place_market_order(
            client=self.client,
            symbol=symbol,
            side="BUY",
            quantity=size["quantity"],
            state=self.state,
        )

        fill_price = float(entry_resp.get("avgPrice", signal["close"]))
        exits = compute_initial_exits(fill_price, signal["atr"])

        # Place stop-loss order
        stop_resp = place_stop_loss_order(
            client=self.client,
            symbol=symbol,
            quantity=size["quantity"],
            stop_price=exits["stop_price"],
            state=self.state,
        )

        # Place TP1 limit order
        tp1_resp = place_limit_tp_order(
            client=self.client,
            symbol=symbol,
            quantity=size["quantity"] * 0.5,  # 50% at TP1
            tp_price=exits["tp1_price"],
            state=self.state,
        )

        stop_order_id = stop_resp.get("orderId") if stop_resp else None
        tp1_order_id = tp1_resp.get("orderId") if tp1_resp else None

        # Record in state
        self.state.setdefault("open_positions", {})[symbol] = {
            "entry_price": fill_price,
            "quantity_total": size["quantity"],
            "quantity_remaining": size["quantity"],
            "stop_price": exits["stop_price"],
            "tp1_price": exits["tp1_price"],
            "tp1_hit": False,
            "trail_atr": exits["trail_atr"],
            "trail_high_watermark": 0.0,
            "trail_stop_price": 0.0,
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "stop_order_id": stop_order_id,
            "tp1_order_id": tp1_order_id,
            "trail_order_id": None,
        }

        save_state(self.state)

        funding_pct = funding_rate * 100
        boost_str = f"BOOST ({funding_pct:.3f}%)" if boost > 1.0 else f"neutral ({funding_pct:.3f}%)"
        regime = self.state.get("btc_regime", "BULL")

        logger.info(
            "[%s] ✅ POSITION OPENED: entry=%.4f | qty=%.4f | stop=%.4f | tp1=%.4f",
            symbol, fill_price, size["quantity"], exits["stop_price"], exits["tp1_price"],
        )

        if self.notifier:
            self.notifier(
                f"🟢 TRADE OPEN\n"
                f"Symbol: {symbol}\n"
                f"Entry: ${fill_price:.2f}\n"
                f"Size: {size['quantity']:.4f} {symbol[:3]} (${size['notional']:.2f})\n"
                f"Stop: ${exits['stop_price']:.2f} (-${(fill_price - exits['stop_price']) * size['quantity']:.2f})\n"
                f"TP1: ${exits['tp1_price']:.2f} (+${(exits['tp1_price'] - fill_price) * size['quantity'] * 0.5:.2f})\n"
                f"Regime: {regime} | Funding: {boost_str}\n"
                f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
            )

    def run_exit_cycle(self, symbol: str, current_price: float) -> None:
        """
        Called on every 4H candle close or price update.
        Update trailing stops and check for triggered exits.
        """
        pos = self.state.get("open_positions", {}).get(symbol)
        if not pos:
            return

        trade = TradeState(
            symbol=symbol,
            entry_price=float(pos["entry_price"]),
            quantity_total=float(pos["quantity_total"]),
            quantity_remaining=float(pos["quantity_remaining"]),
            stop_price=float(pos["stop_price"]),
            tp1_price=float(pos["tp1_price"]),
            tp1_hit=bool(pos.get("tp1_hit", False)),
            trail_atr=float(pos.get("trail_atr", 0.0)),
            trail_high_watermark=float(pos.get("trail_high_watermark", 0.0)),
            trail_stop_price=float(pos.get("trail_stop_price", 0.0)),
            entry_time=pos.get("entry_time", ""),
            stop_order_id=pos.get("stop_order_id"),
            tp1_order_id=pos.get("tp1_order_id"),
            trail_order_id=pos.get("trail_order_id"),
        )

        updated = update_trailing_stop(trade, current_price)

        # Sync updated state back
        pos["trail_high_watermark"] = updated.trail_high_watermark
        pos["trail_stop_price"] = updated.trail_stop_price

        # Check if trailing stop was breached (paper mode: handle manually)
        if trade.tp1_hit and current_price <= updated.trail_stop_price:
            logger.info(
                "[%s] Trailing stop breached: price=%.4f <= trail_stop=%.4f",
                symbol, current_price, updated.trail_stop_price,
            )
            self._close_position(symbol, current_price, "trailing_stop")

        # Check fixed stop loss (paper mode)
        elif not trade.tp1_hit and current_price <= trade.stop_price:
            logger.info(
                "[%s] Fixed stop triggered: price=%.4f <= stop=%.4f",
                symbol, current_price, trade.stop_price,
            )
            self._close_position(symbol, current_price, "stop_loss")

    def _close_position(self, symbol: str, exit_price: float, exit_type: str) -> None:
        """Close a position and record the result."""
        from execution.order_manager import place_market_order
        from state_manager import save_state

        pos = self.state.get("open_positions", {}).get(symbol)
        if not pos:
            return

        qty = float(pos.get("quantity_remaining", 0.0))
        entry = float(pos["entry_price"])
        pnl = (exit_price - entry) * qty
        pnl_pct = ((exit_price - entry) / entry) * 100.0

        # Place close order
        place_market_order(
            client=self.client,
            symbol=symbol,
            side="SELL",
            quantity=qty,
            reduce_only=True,
            state=self.state,
        )

        # Move to closed trades
        closed = dict(pos)
        closed["exit_price"] = exit_price
        closed["pnl"] = pnl
        closed["pnl_pct"] = pnl_pct
        closed["exit_type"] = exit_type
        closed["exit_time"] = datetime.now(timezone.utc).isoformat()

        del self.state["open_positions"][symbol]
        self.state.setdefault("closed_trades", []).append(closed)

        # Update paper P&L
        if not self.config.get("live_trading"):
            self.state["paper_pnl"] = self.state.get("paper_pnl", 0.0) + pnl
            self.state["paper_equity"] = self.state.get("paper_equity", 10000.0) + pnl

        save_state(self.state)

        logger.info(
            "[%s] 🔴 CLOSED (%s): exit=%.4f | P&L=$%.2f (%.2f%%)",
            symbol, exit_type, exit_price, pnl, pnl_pct,
        )

        if self.notifier:
            self.notifier(
                f"🔴 TRADE CLOSED\n"
                f"Symbol: {symbol}\n"
                f"Exit: ${exit_price:.2f} ({exit_type.replace('_', ' ')})\n"
                f"P&L: ${pnl:+.2f} ({pnl_pct:+.2f}%)"
            )
