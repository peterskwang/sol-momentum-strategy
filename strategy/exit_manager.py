"""Exit management utilities."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from config import STOP_ATR_MULT, TP1_ATR_MULT, TP1_CLOSE_PCT, TRAIL_ATR_MULT
from execution.order_manager import notify_tp1_hit, notify_trade_closed


@dataclass
class TradeState:
    symbol: str
    entry_price: float
    atr: float
    qty: float
    stop_price: float
    tp1_price: float
    trailing_stop: float
    remaining_qty: float
    realized_pnl: float = 0.0
    tp1_hit: bool = False
    closed: bool = False
    trail_high_watermark: float | None = field(default=None)
    opened_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def compute_initial_exits(symbol: str, entry_price: float, atr: float, qty: float) -> TradeState:
    stop_price = entry_price - STOP_ATR_MULT * atr
    tp1_price = entry_price + TP1_ATR_MULT * atr
    trailing_stop = entry_price - TRAIL_ATR_MULT * atr
    return TradeState(
        symbol=symbol,
        entry_price=entry_price,
        atr=atr,
        qty=qty,
        stop_price=stop_price,
        tp1_price=tp1_price,
        trailing_stop=trailing_stop,
        remaining_qty=qty,
        trail_high_watermark=None,
    )


def update_trailing_stop(trade: TradeState, current_price: float) -> None:
    if not trade.tp1_hit:
        return
    high_watermark = trade.trail_high_watermark or trade.tp1_price
    if current_price > high_watermark:
        trade.trail_high_watermark = current_price
    new_trail = (trade.trail_high_watermark or high_watermark) - TRAIL_ATR_MULT * trade.atr
    if new_trail > trade.trailing_stop:
        trade.trailing_stop = new_trail


def handle_tp1_fill(trade: TradeState, fill_price: float, *, logger: Any | None = None) -> None:
    if trade.tp1_hit:
        return
    close_qty = trade.qty * TP1_CLOSE_PCT
    trade.remaining_qty -= close_qty
    realized = close_qty * (fill_price - trade.entry_price)
    trade.realized_pnl += realized
    trade.tp1_hit = True
    trade.trail_high_watermark = trade.tp1_price
    trade.trailing_stop = trade.tp1_price - TRAIL_ATR_MULT * trade.atr
    notify_tp1_hit(
        symbol=trade.symbol,
        tp1_price=fill_price,
        realized_pnl=realized,
        remaining_qty=trade.remaining_qty,
        logger=logger,
    )


def handle_stop_fill(trade: TradeState, fill_price: float, *, reason: str, logger: Any | None = None) -> None:
    pnl_delta = trade.remaining_qty * (fill_price - trade.entry_price)
    trade.realized_pnl += pnl_delta
    trade.remaining_qty = 0
    trade.closed = True
    opened_at = datetime.fromisoformat(trade.opened_at)
    if opened_at.tzinfo is None:
        opened_at = opened_at.replace(tzinfo=timezone.utc)
    hold_hours = (datetime.now(timezone.utc) - opened_at).total_seconds() / 3600
    pct = (trade.realized_pnl / (trade.entry_price * trade.qty)) * 100 if trade.qty else 0.0
    notify_trade_closed(
        symbol=trade.symbol,
        exit_price=fill_price,
        reason=reason,
        realized_pnl=trade.realized_pnl,
        pct_return=pct,
        hold_hours=hold_hours,
        logger=logger,
    )
