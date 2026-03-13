"""Position sizing utilities."""
from __future__ import annotations

from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict

from config import (
    MAX_LEVERAGE,
    MAX_PORTFOLIO_NOTIONAL_X,
    MAX_PORTFOLIO_RISK_PCT,
    PAIR_WEIGHTS,
    RISK_PCT,
    STOP_ATR_MULT,
)


def round_step_size(value: float, step_size: float) -> float:
    quant = Decimal(str(step_size))
    return float((Decimal(str(value)) // quant) * quant)


def get_symbol_lot_size(client: Any, symbol: str, cache: Dict[str, Dict[str, float]] | None = None) -> float:
    cache = cache if cache is not None else {}
    if symbol in cache:
        return cache[symbol]["step"]

    info = client.futures_exchange_info()
    for s in info["symbols"]:
        if s["symbol"] == symbol:
            for f in s["filters"]:
                if f["filterType"] == "LOT_SIZE":
                    step = float(f["stepSize"])
                    cache[symbol] = {"step": step}
                    return step
    raise ValueError(f"Symbol {symbol} not found in exchange info")


def compute_position_size(
    symbol: str,
    equity: float,
    price: float,
    atr14: float,
    funding_boost: float,
    lot_step: float,
    stop_atr_mult: float = STOP_ATR_MULT,
    risk_pct: float = RISK_PCT,
    pair_weights: Dict[str, float] | None = None,
    max_leverage: float = MAX_LEVERAGE,
) -> float:
    pair_weights = pair_weights or PAIR_WEIGHTS

    if atr14 <= 0 or price <= 0:
        raise ValueError("ATR and price must be positive")

    pair_weight = pair_weights.get(symbol, 0)
    risk_dollars = equity * risk_pct * pair_weight * funding_boost
    if risk_dollars <= 0:
        return 0.0

    qty = risk_dollars / (stop_atr_mult * atr14)

    # Cap leverage
    max_notional = equity * max_leverage
    notional = qty * price
    if notional > max_notional:
        qty = max_notional / price

    return round_step_size(qty, lot_step)
