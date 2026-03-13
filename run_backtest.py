"""CLI entry point for running the historical backtest."""
from __future__ import annotations

import argparse

from backtest.backtest_sol import run_backtest


def main() -> None:
    parser = argparse.ArgumentParser(description="Run SOL regime momentum backtest")
    parser.add_argument("--symbols", nargs="*", help="Subset of symbols to backtest")
    args = parser.parse_args()

    metrics = run_backtest(symbols=args.symbols or None)

    print("=== Backtest Results ===")
    print(f"Total trades: {metrics.total_trades}")
    print(f"Win rate: {metrics.win_rate:.2%}")
    print(f"Sharpe ratio: {metrics.sharpe_ratio:.2f}")
    print(f"Max drawdown: {metrics.max_drawdown_pct:.2f}%")
    print(f"Total return: {metrics.total_return_pct:.2f}%")
    print(f"Profit factor: {metrics.profit_factor:.2f}")
    print("--- By Symbol ---")
    for symbol, stats in metrics.by_symbol.items():
        print(f"{symbol}: {stats['trades']} trades, {stats['return_pct']:.2f}%")


if __name__ == "__main__":
    main()
