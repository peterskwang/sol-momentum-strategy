"""Live execution entry point for the SOL regime-filtered momentum strategy."""
from __future__ import annotations

import argparse
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from websocket import WebSocketApp

from config import MAX_LEVERAGE, SYMBOLS, load_config
from execution.binance_client import create_client, setup_futures_leverage
from strategy.portfolio import Portfolio
from strategy.regime_filter import update_regime

STATE_PATH = Path("state/strategy_state.json")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run SOL regime momentum live loop")
    parser.add_argument("--live", action="store_true", help="Enable live trading (requires env flag)")
    parser.add_argument("--testnet", action="store_true", help="Use Binance Futures testnet")
    parser.add_argument("--log-level", default="INFO", help="Logging level")
    return parser.parse_args()


def ensure_state_file() -> None:
    if not STATE_PATH.exists():
        STATE_PATH.write_text(json.dumps({
            "version": "1.0",
            "btc_regime": "BULL",
            "open_positions": {},
            "closed_trades": [],
            "paper_mode": True,
            "paper_equity": 10000.0,
            "paper_pnl": 0.0,
            "lot_size_cache": {},
        }, indent=2))


def start_websocket(portfolio: Portfolio, btc_regime_getter) -> WebSocketApp:
    streams = ["solusdt@kline_4h", "ethusdt@kline_4h", "avaxusdt@kline_4h"]
    url = f"wss://fstream.binance.com/stream?streams={'/'.join(streams)}"

    def on_message(_: Any, message: str) -> None:
        data = json.loads(message)
        candle = data.get("data", {}).get("k", {})
        if candle.get("x"):
            regime = btc_regime_getter()
            portfolio.run_signal_cycle(regime)
            portfolio.run_exit_cycle()

    ws_app = WebSocketApp(url, on_message=on_message)
    thread = threading.Thread(target=ws_app.run_forever, daemon=True)
    thread.start()
    return ws_app


def main() -> None:
    args = parse_args()
    cfg = load_config()

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))
    logger = logging.getLogger("run_live")

    live_enabled = cfg.live_trading and args.live
    paper_mode = not live_enabled

    if live_enabled and (not cfg.api_key or not cfg.api_secret):
        raise RuntimeError("Live trading requires API credentials")

    mode_label = "LIVE" if live_enabled else "PAPER"
    logger.info("Starting strategy in %s mode", mode_label)

    ensure_state_file()

    client = create_client(cfg.api_key, cfg.api_secret, testnet=args.testnet)

    for symbol in SYMBOLS:
        setup_futures_leverage(client, symbol, int(MAX_LEVERAGE), logger=logger)

    portfolio = Portfolio(client=client, state_path=STATE_PATH, logger=logger, paper_mode=paper_mode)

    def refresh_regime() -> str:
        state = portfolio.state
        regime = update_regime(client, state, logger=logger)
        portfolio._save_state()
        return regime

    refresh_regime()

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(refresh_regime, "cron", hour=0, minute=5, id="regime")
    scheduler.add_job(lambda: logger.info("Keepalive"), "interval", minutes=30, id="keepalive")
    scheduler.start()

    ws_app = start_websocket(portfolio, lambda: portfolio.state.get("btc_regime", "BULL"))

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        scheduler.shutdown(wait=False)
        ws_app.close()


if __name__ == "__main__":
    main()
