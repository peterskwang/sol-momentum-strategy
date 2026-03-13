"""Signal generation utilities."""
from __future__ import annotations

from typing import Any

import pandas as pd

from config import DONCHIAN_PERIOD, VOLUME_MULTIPLIER
from strategy.regime_filter import REGIME_BEAR


def fetch_4h_klines(client: Any, symbol: str, limit: int = 300) -> pd.DataFrame:
    raw = client.futures_klines(symbol=symbol, interval="4h", limit=limit)
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
    return df.astype({"open": float, "high": float, "low": float, "close": float, "volume": float})


def check_donchian_breakout(highs: pd.Series, closes: pd.Series, period: int = DONCHIAN_PERIOD) -> bool:
    if len(highs) < period + 1 or len(closes) < period + 1:
        raise ValueError("Insufficient data for Donchian breakout check")

    lookback_high = highs.iloc[-period - 1 : -1].max()
    last_close = closes.iloc[-1]
    return last_close > lookback_high


def check_bullish_candle(opens: pd.Series, closes: pd.Series) -> bool:
    if len(opens) < 2 or len(closes) < 2:
        raise ValueError("Need at least two candles for bullish check")
    return closes.iloc[-1] > opens.iloc[-1] and closes.iloc[-1] > closes.iloc[-2]


def check_volume_confirmation(volumes: pd.Series, multiplier: float = VOLUME_MULTIPLIER) -> bool:
    if len(volumes) < 5:
        raise ValueError("Need at least five volume points for confirmation")
    avg_volume = volumes.iloc[-5:-1].mean()
    return volumes.iloc[-1] >= avg_volume * multiplier


def generate_entry_signal(
    symbol: str,
    df: pd.DataFrame,
    btc_regime: str,
    donchian_period: int = DONCHIAN_PERIOD,
    volume_multiplier: float = VOLUME_MULTIPLIER,
) -> bool:
    """Return True if all entry conditions are satisfied."""

    if btc_regime == REGIME_BEAR:
        return False

    highs = df["high"].astype(float)
    closes = df["close"].astype(float)
    opens = df["open"].astype(float)
    volumes = df["volume"].astype(float)

    return (
        check_donchian_breakout(highs, closes, donchian_period)
        and check_bullish_candle(opens, closes)
        and check_volume_confirmation(volumes, volume_multiplier)
    )


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Compute the Average True Range using Wilder's smoothing."""

    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)

    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    return atr
