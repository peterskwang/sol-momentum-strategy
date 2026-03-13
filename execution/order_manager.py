"""
execution/order_manager.py — Order Placement & Management

Place, modify, cancel, and track orders on Binance Futures.
Supports paper trading mode (no real API calls when LIVE_TRADING=false).
"""

import os
import logging
from datetime import datetime, timezone
from typing import Optional

from binance.client import Client
from binance.exceptions import BinanceAPIException

logger = logging.getLogger(__name__)

# Trailing stop callback rate bounds (Binance requirement)
TRAIL_CALLBACK_MIN_PCT = 0.1
TRAIL_CALLBACK_MAX_PCT = 5.0


def _is_paper_mode() -> bool:
    """Check if running in paper mode (default)."""
    return os.environ.get("LIVE_TRADING", "false").lower() != "true"


def _paper_fill_price(client, symbol: str) -> float:
    """Get current mark price for paper mode fills."""
    try:
        data = client.futures_mark_price(symbol=symbol)
        if isinstance(data, list):
            data = data[0]
        return float(data.get("markPrice", 0.0))
    except Exception:
        return 0.0


def _paper_order_id() -> int:
    """Generate a synthetic paper order ID."""
    import random
    return random.randint(10_000_000, 99_999_999)


def place_market_order(
    client,
    symbol: str,
    side: str,
    quantity: float,
    reduce_only: bool = False,
    state: Optional[dict] = None,
) -> dict:
    """
    Place a MARKET order on Binance Futures.

    Args:
        symbol: e.g. "SOLUSDT"
        side: "BUY" or "SELL"
        quantity: base asset quantity (rounded to stepSize)
        reduce_only: True for exit orders only
        state: strategy_state dict (needed for paper mode P&L tracking)

    Returns:
        Order response dict (includes orderId, avgPrice, executedQty, status)
    """
    if _is_paper_mode():
        fill_price = _paper_fill_price(client, symbol)
        order_id = _paper_order_id()
        logger.info(
            "[PAPER] MARKET %s %s qty=%.4f @ fill=%.4f | orderId=%d",
            side, symbol, quantity, fill_price, order_id,
        )
        return {
            "orderId": order_id,
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "executedQty": str(quantity),
            "avgPrice": str(fill_price),
            "status": "FILLED",
            "paper": True,
        }

    # Live mode
    params = dict(
        symbol=symbol,
        side=side,
        type=Client.FUTURE_ORDER_TYPE_MARKET,
        quantity=quantity,
    )
    if reduce_only:
        params["reduceOnly"] = True

    try:
        resp = client.futures_create_order(**params)
        logger.info(
            "[LIVE] MARKET %s %s qty=%.4f @ avg=%.4f | orderId=%d",
            side, symbol, quantity,
            float(resp.get("avgPrice", 0)),
            resp.get("orderId", 0),
        )
        return resp
    except BinanceAPIException as exc:
        logger.error("[%s] Failed to place MARKET %s order: %s", symbol, side, exc)
        raise


def place_stop_loss_order(
    client,
    symbol: str,
    quantity: float,
    stop_price: float,
    state: Optional[dict] = None,
) -> dict:
    """
    Place a STOP_MARKET order as stop loss.

    Notes:
        - type = STOP_MARKET, side = SELL, reduceOnly = True
        - workingType = CONTRACT_PRICE
    """
    if _is_paper_mode():
        order_id = _paper_order_id()
        logger.info(
            "[PAPER] STOP_MARKET SELL %s qty=%.4f @ stop=%.4f | orderId=%d",
            symbol, quantity, stop_price, order_id,
        )
        return {
            "orderId": order_id,
            "symbol": symbol,
            "side": "SELL",
            "type": "STOP_MARKET",
            "stopPrice": str(stop_price),
            "executedQty": "0",
            "status": "NEW",
            "paper": True,
        }

    try:
        resp = client.futures_create_order(
            symbol=symbol,
            side="SELL",
            type="STOP_MARKET",
            quantity=quantity,
            stopPrice=stop_price,
            reduceOnly=True,
            workingType="CONTRACT_PRICE",
        )
        logger.info(
            "[LIVE] STOP_MARKET %s qty=%.4f stop=%.4f | orderId=%d",
            symbol, quantity, stop_price, resp.get("orderId", 0),
        )
        return resp
    except BinanceAPIException as exc:
        logger.error("[%s] Failed to place stop-loss order: %s", symbol, exc)
        raise


def place_limit_tp_order(
    client,
    symbol: str,
    quantity: float,
    tp_price: float,
    state: Optional[dict] = None,
) -> dict:
    """
    Place a LIMIT order for TP1 (take profit).

    Notes:
        - type = LIMIT, side = SELL, timeInForce = GTC, reduceOnly = True
    """
    if _is_paper_mode():
        order_id = _paper_order_id()
        logger.info(
            "[PAPER] LIMIT SELL %s qty=%.4f @ tp=%.4f | orderId=%d",
            symbol, quantity, tp_price, order_id,
        )
        return {
            "orderId": order_id,
            "symbol": symbol,
            "side": "SELL",
            "type": "LIMIT",
            "price": str(tp_price),
            "executedQty": "0",
            "status": "NEW",
            "paper": True,
        }

    try:
        resp = client.futures_create_order(
            symbol=symbol,
            side="SELL",
            type=Client.ORDER_TYPE_LIMIT,
            quantity=quantity,
            price=tp_price,
            timeInForce="GTC",
            reduceOnly=True,
        )
        logger.info(
            "[LIVE] LIMIT TP1 %s qty=%.4f price=%.4f | orderId=%d",
            symbol, quantity, tp_price, resp.get("orderId", 0),
        )
        return resp
    except BinanceAPIException as exc:
        logger.error("[%s] Failed to place TP1 limit order: %s", symbol, exc)
        raise


def place_trailing_stop_order(
    client,
    symbol: str,
    quantity: float,
    callback_rate_pct: float,
) -> dict:
    """
    Place a TRAILING_STOP_MARKET order for remainder after TP1.

    Args:
        callback_rate_pct: trailing distance as percentage (e.g. 1.5 for 1.5%)
                           clamped to [0.1, 5.0] per Binance requirements
    """
    clamped = max(TRAIL_CALLBACK_MIN_PCT, min(TRAIL_CALLBACK_MAX_PCT, callback_rate_pct))
    if clamped != callback_rate_pct:
        logger.warning(
            "Trailing callback rate %.4f%% clamped to %.4f%%",
            callback_rate_pct, clamped,
        )

    if _is_paper_mode():
        order_id = _paper_order_id()
        logger.info(
            "[PAPER] TRAILING_STOP_MARKET SELL %s qty=%.4f callbackRate=%.2f%% | orderId=%d",
            symbol, quantity, clamped, order_id,
        )
        return {
            "orderId": order_id,
            "symbol": symbol,
            "side": "SELL",
            "type": "TRAILING_STOP_MARKET",
            "priceRate": str(clamped),
            "executedQty": "0",
            "status": "NEW",
            "paper": True,
        }

    try:
        resp = client.futures_create_order(
            symbol=symbol,
            side="SELL",
            type="TRAILING_STOP_MARKET",
            quantity=quantity,
            callbackRate=clamped,
            reduceOnly=True,
        )
        logger.info(
            "[LIVE] TRAILING_STOP_MARKET %s qty=%.4f callbackRate=%.2f%% | orderId=%d",
            symbol, quantity, clamped, resp.get("orderId", 0),
        )
        return resp
    except BinanceAPIException as exc:
        logger.error("[%s] Failed to place trailing stop order: %s", symbol, exc)
        raise


def cancel_order(client, symbol: str, order_id: int) -> bool:
    """
    Cancel an existing order.

    Returns:
        True if cancelled, False if not found (already filled/expired)
    """
    if _is_paper_mode():
        logger.info("[PAPER] Cancel order %d for %s", order_id, symbol)
        return True

    try:
        client.futures_cancel_order(symbol=symbol, orderId=order_id)
        logger.info("[LIVE] Cancelled order %d for %s", order_id, symbol)
        return True
    except BinanceAPIException as exc:
        if exc.code == -2011:  # Order not found
            logger.info("[%s] Order %d not found (already filled/expired)", symbol, order_id)
            return False
        logger.error("[%s] Error cancelling order %d: %s", symbol, order_id, exc)
        raise
