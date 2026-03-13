"""Exit management utilities."""
from __future__ import annotations

from dataclasses import dataclass

from config import STOP_ATR_MULT, TP1_ATR_MULT, TP1_CLOSE_PCT, TRAIL_ATR_MULT


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
    tp1_filled: bool = False
    closed: bool = False


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
    )


def update_trailing_stop(trade: TradeState, current_price: float) -> None:
    new_trail = current_price - TRAIL_ATR_MULT * trade.atr
    if new_trail > trade.trailing_stop:
        trade.trailing_stop = new_trail


def handle_tp1_fill(trade: TradeState, fill_price: float) -> None:
    if trade.tp1_filled:
        return
    close_qty = trade.qty * TP1_CLOSE_PCT
    trade.remaining_qty -= close_qty
    trade.realized_pnl += close_qty * (fill_price - trade.entry_price)
    trade.tp1_filled = True


def handle_stop_fill(trade: TradeState, fill_price: float) -> None:
    trade.realized_pnl += trade.remaining_qty * (fill_price - trade.entry_price)
    trade.remaining_qty = 0
    trade.closed = True
