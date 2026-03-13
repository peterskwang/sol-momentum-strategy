"""
strategy/exit_manager.py — Exit Management

Manages stop losses, TP1, and trailing stop logic.
Handles fill events from WebSocket user data stream.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

from config import STOP_ATR_MULT, TP1_ATR_MULT, TRAIL_ATR_MULT

logger = logging.getLogger(__name__)

# Binance TRAILING_STOP_MARKET callback rate limits
TRAIL_CALLBACK_MIN_PCT = 0.1
TRAIL_CALLBACK_MAX_PCT = 5.0


@dataclass
class TradeState:
    symbol: str
    entry_price: float
    quantity_total: float
    quantity_remaining: float
    stop_price: float
    tp1_price: float
    tp1_hit: bool = False
    trail_atr: float = 0.0          # ATR14 at entry (used for trailing)
    trail_high_watermark: float = 0.0  # highest close seen after TP1 hit
    trail_stop_price: float = 0.0    # current trailing stop price
    entry_time: str = ""             # ISO UTC
    side: str = "LONG"
    stop_order_id: Optional[int] = None
    tp1_order_id: Optional[int] = None
    trail_order_id: Optional[int] = None


def compute_initial_exits(entry_price: float, atr14: float) -> dict:
    """
    Compute initial stop loss and TP1 prices at entry.

    Args:
        entry_price: trade entry price
        atr14: ATR(14) value at entry

    Returns:
        {
            "stop_price": entry_price - STOP_ATR_MULT × atr14,
            "tp1_price": entry_price + TP1_ATR_MULT × atr14,
            "trail_atr": atr14,
        }
    """
    return {
        "stop_price": entry_price - STOP_ATR_MULT * atr14,
        "tp1_price": entry_price + TP1_ATR_MULT * atr14,
        "trail_atr": atr14,
    }


def update_trailing_stop(trade: TradeState, current_price: float) -> TradeState:
    """
    Update trailing stop after TP1 hit.

    Logic:
        - If tp1_hit is False: no-op (fixed stop still active)
        - If tp1_hit is True:
            - Update trail_high_watermark = max(trail_high_watermark, current_price)
            - trail_stop_price = trail_high_watermark - (TRAIL_ATR_MULT × trail_atr)
            - If current_price <= trail_stop_price: mark for exit

    Args:
        trade: current TradeState
        current_price: latest mark price

    Returns:
        Updated TradeState (new object)
    """
    if not trade.tp1_hit:
        return trade

    new_watermark = max(trade.trail_high_watermark, current_price)
    new_trail_stop = new_watermark - (TRAIL_ATR_MULT * trade.trail_atr)

    updated = TradeState(
        symbol=trade.symbol,
        entry_price=trade.entry_price,
        quantity_total=trade.quantity_total,
        quantity_remaining=trade.quantity_remaining,
        stop_price=trade.stop_price,
        tp1_price=trade.tp1_price,
        tp1_hit=trade.tp1_hit,
        trail_atr=trade.trail_atr,
        trail_high_watermark=new_watermark,
        trail_stop_price=new_trail_stop,
        entry_time=trade.entry_time,
        side=trade.side,
        stop_order_id=trade.stop_order_id,
        tp1_order_id=trade.tp1_order_id,
        trail_order_id=trade.trail_order_id,
    )

    logger.debug(
        "[%s] Trailing stop: price=%.4f, watermark=%.4f, trail_stop=%.4f",
        trade.symbol, current_price, new_watermark, new_trail_stop,
    )

    if current_price <= new_trail_stop:
        logger.info(
            "[%s] ⚠️  Trailing stop triggered: price=%.4f <= trail_stop=%.4f",
            trade.symbol, current_price, new_trail_stop,
        )

    return updated


def compute_trailing_callback_rate(trail_atr: float, current_price: float) -> float:
    """
    Compute the callback rate percentage for TRAILING_STOP_MARKET order.

    Binance requires callback rate in [0.1%, 5.0%].

    Args:
        trail_atr: ATR value used for trailing
        current_price: current mark price

    Returns:
        callback_rate_pct (clamped to [0.1, 5.0])
    """
    raw_pct = (trail_atr / current_price) * 100.0
    clamped = max(TRAIL_CALLBACK_MIN_PCT, min(TRAIL_CALLBACK_MAX_PCT, raw_pct))

    if clamped != raw_pct:
        logger.warning(
            "Trailing callback rate %.4f%% clamped to %.4f%% (bounds: [%.1f%%, %.1f%%])",
            raw_pct, clamped, TRAIL_CALLBACK_MIN_PCT, TRAIL_CALLBACK_MAX_PCT,
        )

    return clamped


def handle_tp1_fill(trade: TradeState, client, order_manager, notifier=None) -> TradeState:
    """
    Called when TP1 order fill detected via WebSocket user data stream.

    Actions:
        1. Cancel existing fixed stop-loss order
        2. Place new TRAILING_STOP_MARKET order for remaining quantity
        3. Update trade state: tp1_hit=True, quantity_remaining, trail_high_watermark

    Returns:
        Updated TradeState
    """
    logger.info("[%s] TP1 filled at %.4f — activating trailing stop", trade.symbol, trade.tp1_price)

    # Cancel existing fixed stop-loss
    if trade.stop_order_id:
        cancelled = order_manager.cancel_order(client, trade.symbol, trade.stop_order_id)
        if cancelled:
            logger.info("[%s] Cancelled fixed stop-loss order %d", trade.symbol, trade.stop_order_id)
        else:
            logger.warning("[%s] Fixed stop-loss order %d not found (already filled?)", trade.symbol, trade.stop_order_id)

    # Remaining quantity = 50% of total
    qty_remaining = trade.quantity_total * 0.5

    # Compute trailing callback rate
    callback_rate = compute_trailing_callback_rate(trade.trail_atr, trade.tp1_price)

    # Place trailing stop order
    trail_response = order_manager.place_trailing_stop_order(
        client=client,
        symbol=trade.symbol,
        quantity=qty_remaining,
        callback_rate_pct=callback_rate,
    )

    trail_order_id = trail_response.get("orderId") if trail_response else None

    updated = TradeState(
        symbol=trade.symbol,
        entry_price=trade.entry_price,
        quantity_total=trade.quantity_total,
        quantity_remaining=qty_remaining,
        stop_price=trade.stop_price,
        tp1_price=trade.tp1_price,
        tp1_hit=True,
        trail_atr=trade.trail_atr,
        trail_high_watermark=trade.tp1_price,  # start watermark at TP1 fill price
        trail_stop_price=trade.tp1_price - (TRAIL_ATR_MULT * trade.trail_atr),
        entry_time=trade.entry_time,
        side=trade.side,
        stop_order_id=None,
        tp1_order_id=None,
        trail_order_id=trail_order_id,
    )

    if notifier:
        notifier(
            f"🟡 TP1 HIT\n"
            f"Symbol: {trade.symbol}\n"
            f"TP1 filled: ${trade.tp1_price:.4f}\n"
            f"Remaining: {qty_remaining:.4f} {trade.symbol[:3]} | Trailing stop activated"
        )

    return updated


def handle_stop_fill(trade: TradeState, exit_price: float, state: dict, notifier=None) -> None:
    """
    Called when stop-loss order fill detected.

    Actions:
        1. Mark trade as closed in state dict
        2. Log exit with P&L
        3. Send Telegram alert
    """
    pnl = (exit_price - trade.entry_price) * trade.quantity_remaining
    pnl_pct = ((exit_price - trade.entry_price) / trade.entry_price) * 100.0
    hold_time = "N/A"  # Would need to compute from entry_time

    logger.info(
        "[%s] 🔴 TRADE CLOSED at stop: exit=%.4f | P&L=$%.2f (%.2f%%) | qty=%.4f",
        trade.symbol, exit_price, pnl, pnl_pct, trade.quantity_remaining,
    )

    # Remove from open positions in state
    if "open_positions" in state and trade.symbol in state["open_positions"]:
        closed_trade = {**state["open_positions"][trade.symbol]}
        closed_trade["exit_price"] = exit_price
        closed_trade["pnl"] = pnl
        closed_trade["pnl_pct"] = pnl_pct
        closed_trade["exit_type"] = "stop"
        del state["open_positions"][trade.symbol]
        state.setdefault("closed_trades", []).append(closed_trade)

    if notifier:
        notifier(
            f"🔴 TRADE CLOSED\n"
            f"Symbol: {trade.symbol}\n"
            f"Exit: ${exit_price:.4f} (stop loss)\n"
            f"P&L: ${pnl:+.2f} ({pnl_pct:+.2f}%)\n"
            f"Hold time: {hold_time}"
        )
