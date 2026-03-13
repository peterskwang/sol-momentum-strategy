"""
strategy/signal_generator.py — 4H Entry Signal Generator

Computes Donchian breakout + volume confirmation + bullish candle signals
for each trading pair. Returns structured signal dicts for portfolio processing.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd
import pandas_ta as ta

from config import (
    DONCHIAN_PERIOD,
    VOLUME_MULTIPLIER,
    ATR_PERIOD,
    ATR_WARMUP_CANDLES,
    SIGNAL_INTERVAL,
)

logger = logging.getLogger(__name__)


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


def fetch_4h_klines(client, symbol: str, lookback_candles: int = 60) -> pd.DataFrame:
    """
    Fetch recent 4H OHLCV for a symbol.

    Args:
        client: Binance Client
        symbol: e.g. "SOLUSDT"
        lookback_candles: number of 4H candles to fetch (min 50 for signals)

    Returns:
        DataFrame with columns: [open_time, open, high, low, close, volume]
        Index: DatetimeIndex UTC
        - Only completed candles (excludes current open candle)
    """
    raw = client.futures_klines(
        symbol=symbol,
        interval=SIGNAL_INTERVAL,
        limit=lookback_candles + 1,  # +1 so we can drop current incomplete candle
    )

    if not raw:
        raise ValueError(f"No 4H kline data returned for {symbol}")

    df = _klines_to_dataframe(raw)

    # Drop the last row (current incomplete candle)
    df = df.iloc[:-1]

    return df


def check_donchian_breakout(df: pd.DataFrame, period: int = DONCHIAN_PERIOD) -> bool:
    """
    Check if latest closed 4H candle close exceeds 20-period Donchian upper band.

    Args:
        df: OHLCV DataFrame, latest row = most recently completed candle
        period: lookback period for Donchian upper band (default 20)

    Returns:
        True if df['close'].iloc[-1] > df['high'].iloc[-period-1:-1].max()

    Raises:
        ValueError: if fewer than period+2 rows provided
    """
    if len(df) < period + 2:
        raise ValueError(
            f"Insufficient data for Donchian breakout: need {period+2} rows, got {len(df)}"
        )

    # Upper band = max of the 20 HIGH values PRIOR to the current (last) candle
    upper_band = df["high"].iloc[-(period + 1):-1].max()
    current_close = df["close"].iloc[-1]

    result = bool(current_close > upper_band)
    logger.debug(
        "Donchian breakout: close=%.4f, upper_band=%.4f → %s",
        current_close, upper_band, result,
    )
    return result


def check_bullish_candle(df: pd.DataFrame) -> bool:
    """
    Check if latest closed candle is bullish (close > open).

    Args:
        df: OHLCV DataFrame

    Returns:
        True if df['close'].iloc[-1] > df['open'].iloc[-1]
    """
    return bool(df["close"].iloc[-1] > df["open"].iloc[-1])


def check_volume_confirmation(
    df: pd.DataFrame,
    period: int = DONCHIAN_PERIOD,
    multiplier: float = VOLUME_MULTIPLIER,
) -> bool:
    """
    Check if breakout candle volume exceeds 1.5× 20-period average volume.

    Args:
        df: OHLCV DataFrame
        period: lookback period for average volume
        multiplier: volume must exceed avg × multiplier (strict >)

    Returns:
        True if df['volume'].iloc[-1] > df['volume'].iloc[-period-1:-1].mean() * multiplier
    """
    avg_volume = df["volume"].iloc[-(period + 1):-1].mean()
    current_volume = df["volume"].iloc[-1]
    result = bool(current_volume > avg_volume * multiplier)
    logger.debug(
        "Volume check: vol=%.2f, avg_vol=%.2f, threshold=%.2f → %s",
        current_volume, avg_volume, avg_volume * multiplier, result,
    )
    return result


def compute_atr14(df: pd.DataFrame, period: int = ATR_PERIOD) -> float:
    """
    Compute ATR(14) using Wilder's smoothing via pandas-ta.

    Args:
        df: OHLCV DataFrame with at least ATR_WARMUP_CANDLES rows
        period: ATR lookback period (default 14)

    Returns:
        Latest ATR value (float)

    Raises:
        ValueError: if not enough data or ATR is NaN/zero
    """
    if len(df) < ATR_WARMUP_CANDLES:
        raise ValueError(
            f"Insufficient data for ATR: need {ATR_WARMUP_CANDLES} rows, got {len(df)}"
        )

    atr_series = ta.atr(df["high"], df["low"], df["close"], length=period)
    atr_val = atr_series.iloc[-1]

    if pd.isna(atr_val) or atr_val <= 0:
        raise ValueError(f"ATR computed as invalid value: {atr_val}")

    return float(atr_val)


def generate_entry_signal(client, symbol: str, regime: str) -> dict:
    """
    Check all three entry conditions for a symbol. Returns signal result dict.

    Args:
        client: Binance Client
        symbol: e.g. "SOLUSDT"
        regime: "BULL" or "BEAR" (from regime_filter)

    Returns:
        {
            "symbol": str,
            "signal": bool,           # True = enter long
            "reason": str,            # explanation string for logging
            "close": float,           # entry price candidate
            "atr": float,             # ATR14 of most recent closed candle
            "timestamp": str,         # ISO UTC timestamp of candle
            "donchian_break": bool,
            "bullish_candle": bool,
            "volume_ok": bool,
            "regime_ok": bool,
        }
    """
    result = {
        "symbol": symbol,
        "signal": False,
        "reason": "",
        "close": 0.0,
        "atr": 0.0,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "donchian_break": False,
        "bullish_candle": False,
        "volume_ok": False,
        "regime_ok": False,
    }

    # --- Regime gate ---
    regime_ok = regime == "BULL"
    result["regime_ok"] = regime_ok
    if not regime_ok:
        result["reason"] = "BEAR regime — no longs"
        logger.info("[%s] Signal SKIP: %s", symbol, result["reason"])
        return result

    # --- Fetch 4H data ---
    try:
        df = fetch_4h_klines(client, symbol, lookback_candles=60)
    except Exception as exc:
        result["reason"] = f"Failed to fetch 4H klines: {exc}"
        logger.error("[%s] %s", symbol, result["reason"])
        return result

    # --- ATR ---
    try:
        atr = compute_atr14(df)
    except ValueError as exc:
        result["reason"] = f"ATR error: {exc}"
        logger.error("[%s] %s", symbol, result["reason"])
        return result

    result["atr"] = atr
    result["close"] = float(df["close"].iloc[-1])
    result["timestamp"] = df.index[-1].isoformat()

    # --- Individual condition checks ---
    try:
        donchian_break = check_donchian_breakout(df)
    except ValueError as exc:
        result["reason"] = f"Donchian error: {exc}"
        logger.error("[%s] %s", symbol, result["reason"])
        return result

    bullish_candle = check_bullish_candle(df)
    volume_ok = check_volume_confirmation(df)

    result["donchian_break"] = donchian_break
    result["bullish_candle"] = bullish_candle
    result["volume_ok"] = volume_ok

    # --- Combine conditions ---
    if donchian_break and bullish_candle and volume_ok:
        result["signal"] = True
        result["reason"] = "All conditions met: Donchian breakout + bullish candle + volume confirmation"
        logger.info("[%s] ✅ SIGNAL: %s | close=%.4f | ATR=%.4f", symbol, result["reason"], result["close"], atr)
    else:
        reasons = []
        if not donchian_break:
            reasons.append("no Donchian breakout")
        if not bullish_candle:
            reasons.append("candle not bullish")
        if not volume_ok:
            reasons.append("insufficient volume")
        result["reason"] = "Conditions not met: " + ", ".join(reasons)
        logger.debug("[%s] No signal: %s", symbol, result["reason"])

    return result
