"""BTC regime filter utilities."""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Iterable, List

import pandas as pd

from config import BTC_EMA_FAST, BTC_EMA_SLOW, BTC_SYMBOL
from utils.telegram import send_error_alert, send_regime_change_alert

REGIME_BULL = "BULL"
REGIME_BEAR = "BEAR"


def fetch_btc_daily_klines(client: Any, limit: int = 120) -> pd.DataFrame:
    """Fetch BTC daily klines from the Binance client."""

    raw = client.futures_klines(symbol=BTC_SYMBOL, interval="1d", limit=limit)
    if not raw:
        raise ValueError("Empty kline response for BTC")

    columns = [
        "open_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "close_time",
        "quote_asset_volume",
        "number_of_trades",
        "taker_buy_base",
        "taker_buy_quote",
        "ignore",
    ]
    df = pd.DataFrame(raw, columns=columns)
    df["close"] = df["close"].astype(float)
    return df


def compute_regime(df: pd.DataFrame) -> str:
    """Compute BTC regime based on EMA crossover."""

    if df.empty:
        raise ValueError("DataFrame is empty")

    ema_fast = df["close"].ewm(span=BTC_EMA_FAST, adjust=False).mean()
    ema_slow = df["close"].ewm(span=BTC_EMA_SLOW, adjust=False).mean()

    if ema_fast.iloc[-1] >= ema_slow.iloc[-1]:
        return REGIME_BULL
    return REGIME_BEAR


def update_regime(
    client: Any,
    state: Dict[str, Any],
    logger: logging.Logger | None = None,
    max_retries: int = 3,
    backoff_seconds: float = 2.0,
) -> str:
    """Update BTC regime with retries and backoff."""

    attempt = 0
    previous_regime = state.get("btc_regime")
    while True:
        try:
            df = fetch_btc_daily_klines(client)
            regime = compute_regime(df)
            state["btc_regime"] = regime
            if logger:
                logger.info("BTC regime updated to %s", regime)
            if previous_regime and previous_regime != regime:
                send_regime_change_alert(regime, logger=logger)
            return regime
        except Exception as exc:  # noqa: BLE001
            attempt += 1
            if logger:
                logger.warning("Failed to update regime (attempt %s/%s): %s", attempt, max_retries, exc)
            action = "Retrying after backoff" if attempt < max_retries else "Giving up"
            send_error_alert(
                component="RegimeFilter",
                error=str(exc),
                action=action,
                logger=logger,
            )
            if attempt >= max_retries:
                raise
            time.sleep(backoff_seconds * attempt)
