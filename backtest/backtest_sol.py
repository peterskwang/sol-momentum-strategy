"""
backtest/backtest_sol.py — Core Backtest Engine

Vectorized backtest for the SOL Regime-Filtered ATR Momentum Strategy.
Replays 2023-2025 4H kline data across SOL, ETH, AVAX with BTC regime filter.
"""

import os
import time
import logging
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pandas_ta as ta

logger = logging.getLogger(__name__)

BACKTEST_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

# Commission: 0.04% taker fee per leg (entry + exit = 0.08% round-trip)
TAKER_FEE_PCT = 0.0004


def _klines_to_dataframe(raw: list) -> pd.DataFrame:
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


def fetch_klines_paginated(client, symbol: str, interval: str, start_ms: int, end_ms: int, limit: int = 1500) -> list:
    """
    Fetch all klines between start_ms and end_ms, paginating as needed.

    Args:
        client: Binance Client
        symbol: e.g. "SOLUSDT"
        interval: e.g. "4h" or "1d"
        start_ms: start timestamp in milliseconds
        end_ms: end timestamp in milliseconds
        limit: max candles per request (default 1500)

    Returns:
        List of raw kline arrays
    """
    all_klines = []
    current_start = start_ms

    while current_start < end_ms:
        batch = client.futures_klines(
            symbol=symbol,
            interval=interval,
            startTime=current_start,
            endTime=end_ms,
            limit=limit,
        )
        if not batch:
            break
        all_klines.extend(batch)
        current_start = batch[-1][0] + 1  # next candle after last open_time
        time.sleep(0.1)  # courteous rate limiting

    logger.info("Fetched %d klines for %s %s", len(all_klines), symbol, interval)
    return all_klines


def load_or_fetch_data(
    client,
    symbols: list,
    interval: str,
    start: str,
    end: str,
) -> dict:
    """
    Load from cache (CSV) or fetch from Binance REST API.
    Saves to backtest/data/<SYMBOL>_<interval>_<start>_<end>.csv

    Args:
        client: Binance Client
        symbols: list of symbol strings
        interval: e.g. "4h" or "1d"
        start: "2023-01-01"
        end: "2025-12-31"

    Returns:
        Dict mapping symbol -> DataFrame
    """
    os.makedirs(BACKTEST_DATA_DIR, exist_ok=True)
    data = {}

    start_ms = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    end_ms = int(pd.Timestamp(end, tz="UTC").timestamp() * 1000)

    for symbol in symbols:
        cache_path = os.path.join(
            BACKTEST_DATA_DIR,
            f"{symbol}_{interval}_{start.replace('-', '')}_{end.replace('-', '')}.csv",
        )

        if os.path.exists(cache_path):
            logger.info("[%s] Loading cached data from %s", symbol, cache_path)
            df = pd.read_csv(cache_path, index_col="open_time", parse_dates=True)
            df.index = pd.DatetimeIndex(df.index, tz="UTC")
        else:
            logger.info("[%s] Fetching from Binance (%s %s → %s)...", symbol, interval, start, end)
            raw = fetch_klines_paginated(client, symbol, interval, start_ms, end_ms)
            df = _klines_to_dataframe(raw)
            df.to_csv(cache_path)
            logger.info("[%s] Saved to %s", symbol, cache_path)

        data[symbol] = df

    return data


def _compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Compute ATR14, Donchian upper band, and volume average for all candles."""
    df = df.copy()

    # ATR14 (Wilder smoothing via pandas-ta)
    df["atr14"] = ta.atr(df["high"], df["low"], df["close"], length=14)

    # Donchian upper band (20-period max of prior highs, shifted by 1)
    df["donchian_upper"] = df["high"].shift(1).rolling(20).max()

    # 20-period average volume (prior candles, shifted by 1)
    df["avg_volume"] = df["volume"].shift(1).rolling(20).mean()

    return df


def _compute_btc_regime(btc_df: pd.DataFrame) -> pd.Series:
    """
    Compute daily BTC regime. Returns a Series indexed by date with "BULL"/"BEAR".
    Resampled to daily for easy merging with 4H data.
    """
    btc = btc_df.copy()
    btc["ema20"] = btc["close"].ewm(span=20, adjust=False).mean()
    btc["ema50"] = btc["close"].ewm(span=50, adjust=False).mean()
    btc["regime"] = np.where(btc["ema20"] > btc["ema50"], "BULL", "BEAR")
    return btc["regime"]


def run_backtest(
    data: dict,
    btc_data: pd.DataFrame,
    config: dict,
    initial_equity: float = 10000.0,
) -> dict:
    """
    Run vectorized backtest across all symbols.

    Args:
        data: dict of symbol -> 4H DataFrame
        btc_data: BTC daily DataFrame for regime filter
        config: strategy config dict
        initial_equity: starting equity in USDT

    Returns:
        Results dict with metrics and trade log
    """
    from config import (
        SYMBOLS, PAIR_WEIGHTS, RISK_PCT, STOP_ATR_MULT,
        TP1_ATR_MULT, TRAIL_ATR_MULT, VOLUME_MULTIPLIER,
        DONCHIAN_PERIOD, ATR_WARMUP_CANDLES,
    )

    # Build BTC regime series
    btc_regime = _compute_btc_regime(btc_data)

    # Resample BTC regime to daily, forward-fill to get regime at each 4H candle
    btc_regime_daily = btc_regime.resample("1D").last().ffill()

    equity = initial_equity
    equity_curve = [initial_equity]
    all_trades = []

    # Per-symbol open position tracking
    positions = {s: None for s in SYMBOLS}

    # Pre-compute indicators
    symbol_data = {}
    for symbol in SYMBOLS:
        df = data[symbol].copy()
        df = _compute_indicators(df)
        symbol_data[symbol] = df

    # Build combined timeline of 4H candle timestamps
    # Use SOL as the reference timeline
    ref_symbol = SYMBOLS[0]
    ref_df = symbol_data[ref_symbol]
    candle_times = ref_df.index[ATR_WARMUP_CANDLES + DONCHIAN_PERIOD + 2:]

    logger.info(
        "Backtest: %d candles from %s to %s",
        len(candle_times),
        candle_times[0],
        candle_times[-1],
    )

    for ts in candle_times:
        # Get BTC regime for this date
        date_key = ts.normalize()
        # Get the most recent daily regime before this timestamp
        regime_subset = btc_regime_daily[btc_regime_daily.index <= date_key]
        regime = regime_subset.iloc[-1] if not regime_subset.empty else "BEAR"

        for symbol in SYMBOLS:
            df = symbol_data[symbol]

            # Get data up to and including this candle
            if ts not in df.index:
                continue

            loc = df.index.get_loc(ts)
            if loc < ATR_WARMUP_CANDLES + DONCHIAN_PERIOD + 2:
                continue

            row = df.iloc[loc]
            prev_idx = loc - 1
            prev_row = df.iloc[prev_idx]

            # Update open positions first
            pos = positions[symbol]
            if pos is not None:
                close_price = float(row["close"])
                atr = float(row["atr14"]) if not pd.isna(row["atr14"]) else pos["trail_atr"]

                # Check stop loss
                if not pos["tp1_hit"] and close_price <= pos["stop_price"]:
                    pnl = _close_trade(pos, close_price, "stop_loss", equity, TAKER_FEE_PCT)
                    equity += pnl["net_pnl"]
                    all_trades.append({**pos, **pnl, "exit_time": ts, "exit_type": "stop_loss"})
                    positions[symbol] = None

                # Check TP1
                elif not pos["tp1_hit"] and close_price >= pos["tp1_price"]:
                    tp_pnl = _partial_close(pos, close_price, 0.5, TAKER_FEE_PCT)
                    equity += tp_pnl["net_pnl"]
                    pos["tp1_hit"] = True
                    pos["quantity_remaining"] = pos["quantity_total"] * 0.5
                    pos["trail_high_watermark"] = close_price
                    pos["trail_stop_price"] = close_price - (TRAIL_ATR_MULT * pos["trail_atr"])

                # Update trailing stop after TP1
                elif pos["tp1_hit"]:
                    if close_price > pos["trail_high_watermark"]:
                        pos["trail_high_watermark"] = close_price
                        pos["trail_stop_price"] = close_price - (TRAIL_ATR_MULT * pos["trail_atr"])

                    if close_price <= pos["trail_stop_price"]:
                        pnl = _close_trade(pos, close_price, "trailing_stop", equity, TAKER_FEE_PCT)
                        equity += pnl["net_pnl"]
                        all_trades.append({**pos, **pnl, "exit_time": ts, "exit_type": "trailing_stop"})
                        positions[symbol] = None

                continue  # skip entry logic if in position

            # --- Entry signal check ---
            if regime != "BULL":
                continue

            close_val = float(row["close"])
            open_val = float(row["open"])
            volume_val = float(row["volume"])
            atr_val = float(row["atr14"]) if not pd.isna(row["atr14"]) else 0.0
            donchian_upper = float(row["donchian_upper"]) if not pd.isna(row["donchian_upper"]) else 0.0
            avg_vol = float(row["avg_volume"]) if not pd.isna(row["avg_volume"]) else 0.0

            if atr_val <= 0 or donchian_upper <= 0:
                continue

            donchian_break = close_val > donchian_upper
            bullish_candle = close_val > open_val
            volume_ok = volume_val > avg_vol * VOLUME_MULTIPLIER

            if not (donchian_break and bullish_candle and volume_ok):
                continue

            # Compute position size
            weight = PAIR_WEIGHTS.get(symbol, 1.0)
            risk_dollars = equity * RISK_PCT * weight
            stop_distance = STOP_ATR_MULT * atr_val
            quantity = risk_dollars / stop_distance

            if quantity <= 0:
                continue

            notional = quantity * close_val
            leverage = notional / equity
            if leverage > config.get("max_leverage", 3.0):
                quantity = (equity * config.get("max_leverage", 3.0)) / close_val
                notional = quantity * close_val

            entry_fee = notional * TAKER_FEE_PCT
            equity -= entry_fee  # deduct entry commission immediately

            stop_price = close_val - (STOP_ATR_MULT * atr_val)
            tp1_price = close_val + (TP1_ATR_MULT * atr_val)

            positions[symbol] = {
                "symbol": symbol,
                "entry_price": close_val,
                "entry_time": ts,
                "quantity_total": quantity,
                "quantity_remaining": quantity,
                "stop_price": stop_price,
                "tp1_price": tp1_price,
                "tp1_hit": False,
                "trail_atr": atr_val,
                "trail_high_watermark": 0.0,
                "trail_stop_price": 0.0,
                "entry_fee": entry_fee,
            }

        equity_curve.append(equity)

    # Force-close any remaining open positions at last close price
    for symbol, pos in positions.items():
        if pos is not None:
            df = symbol_data[symbol]
            last_close = float(df["close"].iloc[-1])
            pnl = _close_trade(pos, last_close, "end_of_backtest", equity, TAKER_FEE_PCT)
            equity += pnl["net_pnl"]
            all_trades.append({**pos, **pnl, "exit_time": df.index[-1], "exit_type": "end_of_backtest"})

    # Calculate metrics
    metrics = _compute_metrics(all_trades, equity_curve, initial_equity)
    metrics["trade_log"] = all_trades
    metrics["equity_curve"] = equity_curve

    return metrics


def _close_trade(pos: dict, exit_price: float, exit_type: str, current_equity: float, fee_pct: float) -> dict:
    qty = pos["quantity_remaining"]
    gross_pnl = (exit_price - pos["entry_price"]) * qty
    exit_fee = exit_price * qty * fee_pct
    net_pnl = gross_pnl - exit_fee
    return {
        "exit_price": exit_price,
        "gross_pnl": gross_pnl,
        "exit_fee": exit_fee,
        "net_pnl": net_pnl,
        "return_pct": (net_pnl / current_equity) * 100,
    }


def _partial_close(pos: dict, exit_price: float, fraction: float, fee_pct: float) -> dict:
    qty = pos["quantity_total"] * fraction
    gross_pnl = (exit_price - pos["entry_price"]) * qty
    exit_fee = exit_price * qty * fee_pct
    net_pnl = gross_pnl - exit_fee
    return {"net_pnl": net_pnl, "exit_fee": exit_fee}


def _compute_metrics(trades: list, equity_curve: list, initial_equity: float) -> dict:
    """Compute performance metrics from trade log."""
    if not trades:
        return {
            "total_trades": 0,
            "win_rate": 0.0,
            "sharpe_ratio": 0.0,
            "max_drawdown_pct": 0.0,
            "total_return_pct": 0.0,
            "avg_trade_return_pct": 0.0,
            "profit_factor": 0.0,
            "by_symbol": {},
        }

    df = pd.DataFrame(trades)

    # Filter to fully closed trades
    closed = df[df["exit_type"] != "partial"].copy() if "partial" in df["exit_type"].values else df.copy()

    # Exclude TP1 partial fills from trade count (they're captured in trailing stop exit)
    final_exits = closed[closed["exit_type"] != "tp1_partial"] if "tp1_partial" in closed.get("exit_type", pd.Series()).values else closed

    total = len(final_exits)
    wins = (final_exits["net_pnl"] > 0).sum()
    losses = (final_exits["net_pnl"] <= 0).sum()

    gross_profit = final_exits[final_exits["net_pnl"] > 0]["net_pnl"].sum()
    gross_loss = abs(final_exits[final_exits["net_pnl"] < 0]["net_pnl"].sum())
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    final_equity = equity_curve[-1]
    total_return_pct = ((final_equity - initial_equity) / initial_equity) * 100.0

    # Max drawdown
    peak = initial_equity
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100.0
        if dd > max_dd:
            max_dd = dd

    # Sharpe ratio (annualized from per-trade returns, assuming 4H candles, ~6/day, ~2190/year)
    returns = final_exits["net_pnl"] / initial_equity
    if len(returns) > 1 and returns.std() > 0:
        # Approximate annualization: trades per year ~ total_trades in ~2yr backtest / 2
        trades_per_year = max(total / 2.0, 1)
        sharpe = (returns.mean() / returns.std()) * (trades_per_year ** 0.5)
    else:
        sharpe = 0.0

    # Per-symbol breakdown
    by_symbol = {}
    for symbol in ["SOLUSDT", "ETHUSDT", "AVAXUSDT"]:
        sym_trades = final_exits[final_exits["symbol"] == symbol]
        by_symbol[symbol] = {
            "total_trades": len(sym_trades),
            "win_rate": float((sym_trades["net_pnl"] > 0).mean()) if len(sym_trades) > 0 else 0.0,
            "total_pnl": float(sym_trades["net_pnl"].sum()),
        }

    return {
        "total_trades": total,
        "win_rate": float(wins / total) if total > 0 else 0.0,
        "sharpe_ratio": float(sharpe),
        "max_drawdown_pct": float(max_dd),
        "total_return_pct": float(total_return_pct),
        "avg_trade_return_pct": float(returns.mean() * 100) if len(returns) > 0 else 0.0,
        "profit_factor": float(profit_factor),
        "by_symbol": by_symbol,
    }
