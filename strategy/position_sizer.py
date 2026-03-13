"""
strategy/position_sizer.py — Inverse-Volatility Weighted Position Sizing

Computes position sizes for each pair based on ATR, account equity,
pair weights, and funding rate boost. Enforces leverage caps and lot size
rounding per Binance exchange rules.
"""

import math
import logging
from typing import Optional

from config import (
    RISK_PCT,
    PAIR_WEIGHTS,
    STOP_ATR_MULT,
    TP1_ATR_MULT,
    MAX_LEVERAGE,
)

logger = logging.getLogger(__name__)

# Module-level lot size cache to avoid repeated API calls
_lot_size_cache: dict = {}


def get_symbol_lot_size(client, symbol: str) -> dict:
    """
    Fetch symbol's lot size filter (stepSize, minQty, maxQty) from exchange info.

    Args:
        client: Binance Client
        symbol: e.g. "SOLUSDT"

    Returns:
        {"stepSize": float, "minQty": float, "maxQty": float}
    """
    global _lot_size_cache

    if symbol in _lot_size_cache:
        return _lot_size_cache[symbol]

    exchange_info = client.futures_exchange_info()
    for sym_info in exchange_info.get("symbols", []):
        if sym_info["symbol"] == symbol:
            for f in sym_info.get("filters", []):
                if f["filterType"] == "LOT_SIZE":
                    result = {
                        "stepSize": float(f["stepSize"]),
                        "minQty": float(f["minQty"]),
                        "maxQty": float(f["maxQty"]),
                    }
                    _lot_size_cache[symbol] = result
                    logger.debug("[%s] Lot size: %s", symbol, result)
                    return result

    # Fallback: safe defaults
    logger.warning("[%s] LOT_SIZE filter not found, using defaults", symbol)
    fallback = {"stepSize": 0.1, "minQty": 0.1, "maxQty": 100000.0}
    _lot_size_cache[symbol] = fallback
    return fallback


def round_step_size(quantity: float, step_size: float) -> float:
    """
    Round a quantity to the nearest valid step size (floor rounding).

    Args:
        quantity: raw computed quantity
        step_size: e.g. 0.1 for SOL, 0.001 for ETH

    Returns:
        Floored quantity aligned to stepSize
    """
    if step_size <= 0:
        return quantity
    precision = max(0, -int(math.floor(math.log10(step_size))))
    floored = math.floor(quantity / step_size) * step_size
    return round(floored, precision)


def compute_position_size(
    account_equity: float,
    symbol: str,
    atr14: float,
    current_price: float,
    funding_boost: float = 1.0,
    pair_weight: Optional[float] = None,
    risk_pct: float = RISK_PCT,
    stop_atr_mult: float = STOP_ATR_MULT,
    max_leverage: float = MAX_LEVERAGE,
    contract_multiplier: float = 1.0,
    lot_size: Optional[dict] = None,
) -> dict:
    """
    Compute position size using inverse-volatility weighting formula.

    Formula:
        risk_dollars = account_equity × risk_pct × pair_weight × funding_boost
        stop_distance = stop_atr_mult × atr14 × contract_multiplier
        quantity = risk_dollars / stop_distance
        notional = quantity × current_price
        leverage = notional / account_equity

    Args:
        account_equity: USDT account equity
        symbol: e.g. "SOLUSDT"
        atr14: ATR(14) value in price units
        current_price: current mark price
        funding_boost: 1.0 or 1.2 (from get_funding_boost)
        pair_weight: override weight (default: from PAIR_WEIGHTS config)
        risk_pct: risk per trade (default 1%)
        stop_atr_mult: ATR multiplier for stop distance (default 1.5)
        max_leverage: hard leverage cap (default 3×)
        contract_multiplier: 1.0 for USDT-M perpetuals
        lot_size: optional pre-fetched lot size dict

    Returns:
        {
            "quantity": float,       # base asset quantity
            "notional": float,       # USDT notional
            "leverage": float,       # effective leverage
            "stop_loss": float,      # absolute stop price
            "tp1_price": float,      # TP1 absolute price
            "risk_dollars": float,   # dollar risk on this trade
            "capped": bool,          # True if leverage was capped
        }

    Raises:
        ValueError: if atr14 <= 0 or account_equity <= 0
    """
    if atr14 <= 0:
        raise ValueError(f"Invalid ATR14 value: {atr14} (must be > 0)")
    if account_equity <= 0:
        raise ValueError(f"Invalid account equity: {account_equity} (must be > 0)")

    weight = pair_weight if pair_weight is not None else PAIR_WEIGHTS.get(symbol, 1.0)

    risk_dollars = account_equity * risk_pct * weight * funding_boost
    stop_distance = stop_atr_mult * atr14 * contract_multiplier
    quantity = risk_dollars / stop_distance

    notional = quantity * current_price
    leverage = notional / account_equity

    capped = False
    if leverage > max_leverage:
        # Scale down quantity to fit within leverage cap
        max_notional = account_equity * max_leverage
        quantity = max_notional / current_price
        notional = max_notional
        leverage = max_leverage
        capped = True
        logger.info(
            "[%s] Leverage capped at %.1f× — scaled quantity to %.4f",
            symbol, max_leverage, quantity,
        )

    # Round to lot size if provided
    if lot_size:
        step_size = lot_size["stepSize"]
        min_qty = lot_size["minQty"]
        quantity = round_step_size(quantity, step_size)
        if quantity < min_qty:
            logger.info(
                "[%s] Quantity %.6f below minQty %.6f — rejecting trade",
                symbol, quantity, min_qty,
            )
            return {
                "quantity": 0.0,
                "notional": 0.0,
                "leverage": 0.0,
                "stop_loss": 0.0,
                "tp1_price": 0.0,
                "risk_dollars": 0.0,
                "capped": capped,
            }
        # Recompute notional/leverage after rounding
        notional = quantity * current_price
        leverage = notional / account_equity

    stop_loss = current_price - (stop_atr_mult * atr14)
    tp1_price = current_price + (TP1_ATR_MULT * atr14)

    logger.info(
        "[%s] Position size: qty=%.4f | notional=$%.2f | leverage=%.2f× | "
        "stop=$%.4f | TP1=$%.4f | risk=$%.2f%s",
        symbol, quantity, notional, leverage,
        stop_loss, tp1_price, risk_dollars,
        " [CAPPED]" if capped else "",
    )

    return {
        "quantity": quantity,
        "notional": notional,
        "leverage": leverage,
        "stop_loss": stop_loss,
        "tp1_price": tp1_price,
        "risk_dollars": risk_dollars,
        "capped": capped,
    }
