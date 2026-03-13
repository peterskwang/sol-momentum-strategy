"""Backtest engine for the SOL regime-filtered ATR momentum strategy."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

import pandas as pd
import requests

from config import ATR_PERIOD, DONCHIAN_PERIOD, STOP_ATR_MULT, SYMBOLS, TP1_ATR_MULT
from strategy.signal_generator import compute_atr

DATA_DIR = Path("backtest/data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

BINANCE_FAPI_URL = "https://fapi.binance.com/fapi/v1/klines"
TAKER_FEE = 0.0004


@dataclass
class BacktestMetrics:
    total_trades: int
    win_rate: float
    sharpe_ratio: float
    max_drawdown_pct: float
    total_return_pct: float
    profit_factor: float
    by_symbol: Dict[str, Dict[str, float]]


def load_or_fetch_data(symbol: str, start: str, end: str, interval: str = "4h") -> pd.DataFrame:
    cache_path = DATA_DIR / f"{symbol}_{interval}.csv"
    if cache_path.exists():
        return pd.read_csv(cache_path)

    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": 1500,
    }
    resp = requests.get(BINANCE_FAPI_URL, params=params, timeout=10)
    resp.raise_for_status()
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
    df = pd.DataFrame(resp.json(), columns=columns)
    df.to_csv(cache_path, index=False)
    return df


def _compute_sharpe(returns: pd.Series) -> float:
    if returns.std() == 0:
        return 0.0
    return (returns.mean() / returns.std()) * (365 ** 0.5)


def _compute_drawdown(equity_curve: pd.Series) -> float:
    running_max = equity_curve.cummax()
    dd = (equity_curve - running_max) / running_max
    return dd.min() * 100


def run_backtest(symbols: List[str] | None = None) -> BacktestMetrics:
    symbols = symbols or SYMBOLS
    trade_returns = []
    symbol_stats: Dict[str, List[float]] = {s: [] for s in symbols}

    for symbol in symbols:
        df = load_or_fetch_data(symbol, "2023-01-01", "2025-12-31")
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)

        df["atr"] = compute_atr(df, ATR_PERIOD)
        df["donchian"] = df["close"] > df["high"].rolling(DONCHIAN_PERIOD).max().shift(1)
        df["bullish"] = (df["close"] > df["open"]) & (df["close"] > df["close"].shift(1))
        df["volume_confirm"] = df["volume"] >= df["volume"].rolling(5).mean().shift(1)
        df["signal"] = df[["donchian", "bullish", "volume_confirm"]].all(axis=1)

        for idx, row in df[df["signal"]].iterrows():
            if idx + 1 >= len(df):
                continue
            entry_price = row["close"] * (1 + TAKER_FEE)
            atr = row["atr"]
            stop = entry_price - STOP_ATR_MULT * atr
            tp = entry_price + TP1_ATR_MULT * atr
            next_bar = df.iloc[idx + 1]
            exit_price = next_bar["close"] * (1 - TAKER_FEE)
            result = 0.0
            if next_bar["high"] >= tp:
                result = (tp - entry_price) / entry_price
            elif next_bar["low"] <= stop:
                result = (stop - entry_price) / entry_price
            else:
                result = (exit_price - entry_price) / entry_price
            trade_returns.append(result)
            symbol_stats[symbol].append(result)

    if not trade_returns:
        return BacktestMetrics(0, 0.0, 0.0, 0.0, 0.0, 0.0, {s: {"trades": 0, "return_pct": 0.0} for s in symbols})

    returns = pd.Series(trade_returns)
    wins = (returns > 0).sum()
    total_return = (1 + returns).prod() - 1
    equity_curve = (1 + returns).cumprod()
    metrics = BacktestMetrics(
        total_trades=len(trade_returns),
        win_rate=wins / len(trade_returns),
        sharpe_ratio=_compute_sharpe(returns),
        max_drawdown_pct=_compute_drawdown(equity_curve),
        total_return_pct=total_return * 100,
        profit_factor=returns[returns > 0].sum() / abs(returns[returns < 0].sum()) if (returns[returns < 0].sum()) != 0 else float("inf"),
        by_symbol={
            symbol: {
                "trades": len(vals),
                "return_pct": ((1 + pd.Series(vals)).prod() - 1) * 100 if vals else 0.0,
            }
            for symbol, vals in symbol_stats.items()
        },
    )
    return metrics
