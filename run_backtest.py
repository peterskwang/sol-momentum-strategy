"""
run_backtest.py — Backtest Runner for SOL Regime-Filtered ATR Momentum Strategy

Fetches 2023-2025 historical data (cached to backtest/data/),
runs vectorized backtest, and prints performance metrics.

Usage:
    python run_backtest.py
    python run_backtest.py --start 2023-01-01 --end 2025-12-31
    python run_backtest.py --equity 50000
"""

import argparse
import json
import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()
load_dotenv("secrets/binance_sol_strategy.env")


def parse_args():
    parser = argparse.ArgumentParser(description="SOL Momentum Strategy Backtest")
    parser.add_argument("--start", default="2023-01-01", help="Backtest start date (YYYY-MM-DD)")
    parser.add_argument("--end", default="2025-12-31", help="Backtest end date (YYYY-MM-DD)")
    parser.add_argument("--equity", type=float, default=10000.0, help="Initial equity (USDT)")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING"], default="INFO")
    return parser.parse_args()


def setup_logging(level: str):
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s UTC | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("binance").setLevel(logging.WARNING)


def main():
    args = parse_args()
    setup_logging(args.log_level)
    logger = logging.getLogger("run_backtest")

    from config import load_config, validate_config, SYMBOLS, BTC_SYMBOL
    from backtest.backtest_sol import load_or_fetch_data

    config = load_config()

    # API keys required to fetch data (can use read-only keys)
    try:
        validate_config(config, require_keys=True)
    except EnvironmentError as exc:
        logger.error("❌  %s", exc)
        sys.exit(1)

    from execution.binance_client import create_client

    try:
        client = create_client(testnet=False)
    except Exception as exc:
        logger.error("❌  Failed to connect to Binance: %s", exc)
        sys.exit(1)

    logger.info("=" * 60)
    logger.info("SOL Momentum Strategy Backtest")
    logger.info("Period: %s → %s | Initial equity: $%.2f", args.start, args.end, args.equity)
    logger.info("=" * 60)

    # Fetch 4H data for trading symbols
    logger.info("Loading 4H data for %s...", SYMBOLS)
    symbol_data = load_or_fetch_data(
        client=client,
        symbols=SYMBOLS,
        interval="4h",
        start=args.start,
        end=args.end,
    )

    # Fetch BTC daily data (with extra lookback for EMA warmup)
    btc_start = "2022-11-01"  # extra lookback for EMA50 warmup
    logger.info("Loading BTC daily data (from %s for EMA warmup)...", btc_start)
    btc_data_dict = load_or_fetch_data(
        client=client,
        symbols=[BTC_SYMBOL],
        interval="1d",
        start=btc_start,
        end=args.end,
    )
    btc_data = btc_data_dict[BTC_SYMBOL]

    # Run backtest
    logger.info("Running backtest...")
    from backtest.backtest_sol import run_backtest

    results = run_backtest(
        data=symbol_data,
        btc_data=btc_data,
        config=config,
        initial_equity=args.equity,
    )

    # Print results
    print("\n" + "=" * 60)
    print("BACKTEST RESULTS")
    print("=" * 60)
    print(f"Period:           {args.start} → {args.end}")
    print(f"Initial equity:   ${args.equity:,.2f}")
    print(f"Final equity:     ${args.equity * (1 + results['total_return_pct'] / 100):,.2f}")
    print(f"Total return:     {results['total_return_pct']:+.2f}%")
    print(f"Total trades:     {results['total_trades']}")
    print(f"Win rate:         {results['win_rate']:.1%}")
    print(f"Sharpe ratio:     {results['sharpe_ratio']:.3f}")
    print(f"Max drawdown:     {results['max_drawdown_pct']:.2f}%")
    print(f"Avg trade return: {results['avg_trade_return_pct']:+.4f}%")
    print(f"Profit factor:    {results['profit_factor']:.3f}")
    print()
    print("Per-symbol breakdown:")
    for symbol, sym_stats in results["by_symbol"].items():
        print(f"  {symbol}: {sym_stats['total_trades']} trades | "
              f"win rate {sym_stats['win_rate']:.1%} | "
              f"P&L ${sym_stats['total_pnl']:+.2f}")

    print("=" * 60)

    # Acceptance check
    print("\nAcceptance criteria:")
    sharpe_ok = 1.0 <= results["sharpe_ratio"] <= 1.6
    dd_ok = 12.0 <= results["max_drawdown_pct"] <= 25.0
    print(f"  Sharpe 1.0–1.6:  {'✅' if sharpe_ok else '❌'} {results['sharpe_ratio']:.3f}")
    print(f"  MaxDD 12–25%:    {'✅' if dd_ok else '❌'} {results['max_drawdown_pct']:.2f}%")

    for symbol in SYMBOLS:
        min_trades = results["by_symbol"].get(symbol, {}).get("total_trades", 0)
        trades_ok = min_trades >= 20
        print(f"  {symbol} ≥20 trades: {'✅' if trades_ok else '❌'} {min_trades}")

    # Save results
    output_path = "backtest/results.json"
    with open(output_path, "w") as f:
        # Remove non-serializable trade_log for JSON output
        save_results = {k: v for k, v in results.items() if k not in ("trade_log", "equity_curve")}
        json.dump(save_results, f, indent=2, default=str)
    logger.info("Results saved to %s", output_path)


if __name__ == "__main__":
    main()
