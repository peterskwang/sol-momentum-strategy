"""
strategy/regime_filter.py — BTC Macro Regime Filter

Fetches BTC daily OHLCV, computes EMA20/EMA50, determines BULL/BEAR regime.
Regime is cached in strategy_state.json and refreshed daily at 00:05 UTC.
"""

import time
import logging
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from config import BTC_SYMBOL, BTC_EMA_FAST, BTC_EMA_SLOW

logger = logging.getLogger(__name__)


def fetch_btc_daily_klines(client, lookback_days: int = 60) -> pd.DataFrame:
    """
    Fetch BTC/USDT daily OHLCV from Binance Futures.

    Args:
        client: Binance Client instance
        lookback_days: Number of daily candles to fetch (min 55 for EMA50 warmup)

    Returns:
        DataFrame with columns: [open_time, open, high, low, close, volume]
        Index: DatetimeIndex in UTC

    Raises:
        BinanceAPIException: on API error
        ValueError: if fewer than 55 candles returned
    """
    raw = client.futures_klines(
        symbol=BTC_SYMBOL,
        interval="1d",
        limit=lookback_days + 1,  # +1 so we can drop current incomplete candle
    )

    if not raw:
        raise ValueError("No kline data returned from Binance for BTCUSDT daily")

    df = _klines_to_dataframe(raw)

    # Drop the last (current, incomplete) candle
    df = df.iloc[:-1]

    if len(df) < 55:
        raise ValueError(
            f"Insufficient BTC daily data: need ≥55 candles, got {len(df)}"
        )

    return df


def _klines_to_dataframe(raw: list) -> pd.DataFrame:
    """Convert raw Binance kline list to a typed DataFrame."""
    df = pd.DataFrame(raw, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "number_of_trades",
        "taker_buy_base_volume", "taker_buy_quote_volume", "ignore",
    ])
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df = df.set_index("open_time")
    return df[["open", "high", "low", "close", "volume"]]


def compute_regime(df: pd.DataFrame) -> str:
    """
    Compute BTC macro regime from daily OHLCV.

    Args:
        df: DataFrame with 'close' column, at least 55 rows

    Returns:
        "BULL" if EMA20 > EMA50 on latest complete candle
        "BEAR" otherwise
    """
    ema_fast = df["close"].ewm(span=BTC_EMA_FAST, adjust=False).mean()
    ema_slow = df["close"].ewm(span=BTC_EMA_SLOW, adjust=False).mean()

    latest_fast = ema_fast.iloc[-1]
    latest_slow = ema_slow.iloc[-1]

    regime = "BULL" if latest_fast > latest_slow else "BEAR"

    logger.debug(
        "BTC EMA%d=%.2f, EMA%d=%.2f → %s",
        BTC_EMA_FAST, latest_fast,
        BTC_EMA_SLOW, latest_slow,
        regime,
    )
    return regime


def update_regime(client, state: dict, notifier=None) -> str:
    """
    High-level: fetch BTC daily data, compute regime, update state dict in-place.

    Args:
        client: Binance Client
        state: Current strategy_state dict (will be mutated)
        notifier: Optional callable(msg: str) for Telegram alerts

    Returns:
        New regime string ("BULL" or "BEAR")

    Side effects:
        - Updates state['btc_regime'] and state['regime_updated_at']
        - Sends Telegram alert if regime changed
        - Logs regime status at INFO level

    Error handling:
        - On API failure: retry 3× with 5s backoff
        - If all retries fail: log ERROR, return last known regime from state
    """
    max_retries = 3
    backoff_s = 5.0
    last_error: Optional[Exception] = None

    for attempt in range(1, max_retries + 1):
        try:
            df = fetch_btc_daily_klines(client, lookback_days=60)
            new_regime = compute_regime(df)
            break
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Regime update attempt %d/%d failed: %s",
                attempt, max_retries, exc,
            )
            if notifier:
                notifier(
                    f"⚠️ ERROR\nComponent: regime_filter\n"
                    f"Error: {exc}\n"
                    f"Action: Retrying in {int(backoff_s)}s (attempt {attempt}/{max_retries})"
                )
            if attempt < max_retries:
                time.sleep(backoff_s)
    else:
        # All retries exhausted
        fallback = state.get("btc_regime", "BEAR")
        logger.error(
            "All regime update attempts failed. Last error: %s. "
            "Using last known regime: %s",
            last_error, fallback,
        )
        return fallback

    old_regime = state.get("btc_regime")
    state["btc_regime"] = new_regime
    state["regime_updated_at"] = datetime.now(timezone.utc).isoformat()

    if old_regime and old_regime != new_regime:
        msg = (
            f"🔄 REGIME CHANGE\n"
            f"BTC EMA{BTC_EMA_FAST}/EMA{BTC_EMA_SLOW} crossed\n"
            f"New regime: {new_regime}\n"
            f"Time: {state['regime_updated_at']}"
        )
        logger.info("Regime changed: %s → %s", old_regime, new_regime)
        if notifier:
            notifier(msg)
    else:
        logger.info("Regime updated: %s (unchanged)", new_regime)

    return new_regime
