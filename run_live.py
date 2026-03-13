"""
run_live.py — Main Entry Point for SOL Regime-Filtered ATR Momentum Strategy

Usage:
    python run_live.py              # paper mode (default)
    python run_live.py --live       # live trading (LIVE_TRADING=true also required)
    python run_live.py --testnet    # use Binance testnet
    python run_live.py --log-level DEBUG

CRITICAL: Paper mode is the default. Live trading requires BOTH:
    1. LIVE_TRADING=true environment variable
    2. --live CLI flag
"""

import argparse
import logging
import os
import sys
import time
import json
from datetime import datetime, timezone

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Load .env before importing config
# ---------------------------------------------------------------------------
load_dotenv()
load_dotenv("secrets/binance_sol_strategy.env")


def parse_args():
    parser = argparse.ArgumentParser(description="SOL Regime-Filtered ATR Momentum Strategy")
    parser.add_argument("--live", action="store_true", help="Enable live trading (requires LIVE_TRADING=true env var)")
    parser.add_argument("--testnet", action="store_true", help="Use Binance testnet")
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Logging level",
    )
    return parser.parse_args()


def setup_logging(log_level: str, log_file: str):
    """Configure logging to both console and file."""
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    fmt = "%(asctime)s UTC | %(levelname)s | %(name)s | %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file, encoding="utf-8"),
    ]

    logging.basicConfig(
        level=getattr(logging, log_level),
        format=fmt,
        datefmt=datefmt,
        handlers=handlers,
    )

    # Suppress noisy third-party loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("binance").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


def main():
    args = parse_args()

    from config import load_config, validate_config, LOG_FILE, STATE_FILE, SYMBOLS, MIN_LEVERAGE

    config = load_config()
    setup_logging(args.log_level, config["log_file"])
    logger = logging.getLogger("run_live")

    # ---------------------------------------------------------------------------
    # Determine trading mode — belt and suspenders
    # ---------------------------------------------------------------------------
    live_trading = args.live and (os.environ.get("LIVE_TRADING", "false").lower() == "true")

    if args.live and not os.environ.get("LIVE_TRADING", "false").lower() == "true":
        logger.error(
            "❌  --live flag provided but LIVE_TRADING env var is not 'true'. "
            "Both are required for live trading. Aborting."
        )
        sys.exit(1)

    if not args.live and os.environ.get("LIVE_TRADING", "false").lower() == "true":
        logger.warning(
            "⚠️  LIVE_TRADING=true is set but --live flag is missing. "
            "Running in PAPER MODE. Add --live flag to enable live trading."
        )
        live_trading = False

    config["live_trading"] = live_trading
    testnet = args.testnet or config.get("testnet", False)

    # ---------------------------------------------------------------------------
    # Display mode banner
    # ---------------------------------------------------------------------------
    if live_trading:
        banner = "\n" + "=" * 60 + "\n🔴  LIVE TRADING MODE — REAL ORDERS WILL BE PLACED  🔴\n" + "=" * 60
    else:
        banner = "\n" + "=" * 60 + "\n⚠️   PAPER MODE ACTIVE — NO REAL ORDERS WILL BE PLACED  ⚠️\n" + "=" * 60

    logger.info(banner)
    print(banner)

    # ---------------------------------------------------------------------------
    # Validate API keys
    # ---------------------------------------------------------------------------
    try:
        validate_config(config, require_keys=True)
    except EnvironmentError as exc:
        logger.error("❌  Configuration error: %s", exc)
        sys.exit(1)

    # ---------------------------------------------------------------------------
    # Create Binance client
    # ---------------------------------------------------------------------------
    from execution.binance_client import create_client, setup_futures_leverage

    try:
        client = create_client(testnet=testnet)
    except (EnvironmentError, ConnectionError) as exc:
        logger.error("❌  Failed to create Binance client: %s", exc)
        sys.exit(1)

    # ---------------------------------------------------------------------------
    # Load state
    # ---------------------------------------------------------------------------
    from state_manager import load_state, save_state

    state = load_state(config["state_file"])
    state["paper_mode"] = not live_trading
    if not live_trading and state.get("paper_equity", 0) <= 0:
        state["paper_equity"] = config["paper_initial_equity"]
    save_state(state)

    # ---------------------------------------------------------------------------
    # Set up notifier
    # ---------------------------------------------------------------------------
    from notifications import create_notifier

    notifier = create_notifier(config)
    if not live_trading:
        notifier(
            f"⚠️ PAPER MODE ACTIVE — no real orders will be placed\n"
            f"Paper equity: ${state.get('paper_equity', 10000):.2f}\n"
            f"Started: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )

    # ---------------------------------------------------------------------------
    # Set leverage for all symbols (live mode only)
    # ---------------------------------------------------------------------------
    if live_trading:
        for symbol in SYMBOLS:
            try:
                setup_futures_leverage(client, symbol, int(MIN_LEVERAGE))
            except Exception as exc:
                logger.warning("[%s] Failed to set leverage: %s", symbol, exc)

    # ---------------------------------------------------------------------------
    # Initial regime check
    # ---------------------------------------------------------------------------
    from strategy.regime_filter import update_regime

    logger.info("Running initial regime check...")
    try:
        update_regime(client, state, notifier=notifier)
        save_state(state)
        logger.info("Initial regime: %s", state.get("btc_regime", "UNKNOWN"))
    except Exception as exc:
        logger.error("Initial regime check failed: %s — defaulting to BEAR", exc)
        state["btc_regime"] = state.get("btc_regime", "BEAR")

    # ---------------------------------------------------------------------------
    # Portfolio
    # ---------------------------------------------------------------------------
    from execution.order_manager import place_market_order, place_stop_loss_order, place_limit_tp_order
    from strategy.portfolio import Portfolio

    portfolio = Portfolio(
        client=client,
        state=state,
        config=config,
        notifier=notifier,
    )

    # ---------------------------------------------------------------------------
    # APScheduler setup
    # ---------------------------------------------------------------------------
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    scheduler = BackgroundScheduler(timezone="UTC")

    def scheduled_regime_update():
        logger.info("Scheduled regime update triggered (00:05 UTC)")
        try:
            update_regime(client, state, notifier=notifier)
            save_state(state)
        except Exception as exc:
            logger.error("Scheduled regime update failed: %s", exc)

    scheduler.add_job(
        scheduled_regime_update,
        CronTrigger(hour=config["regime_check_hour"], minute=config["regime_check_minute"], timezone="UTC"),
        id="regime_update",
        name="Daily Regime Update",
    )

    scheduler.start()
    logger.info("APScheduler started (regime update at %02d:%02d UTC)",
                config["regime_check_hour"], config["regime_check_minute"])

    # ---------------------------------------------------------------------------
    # WebSocket subscription
    # ---------------------------------------------------------------------------
    from binance import ThreadedWebsocketManager

    twm = ThreadedWebsocketManager(
        api_key=os.environ.get("BINANCE_API_KEY", ""),
        api_secret=os.environ.get("BINANCE_API_SECRET", ""),
        testnet=testnet,
    )
    twm.start()

    ws_reconnect_backoff = 1
    MAX_WS_BACKOFF = 60

    def on_kline_message(msg):
        """Handle incoming kline WebSocket messages."""
        nonlocal ws_reconnect_backoff

        if msg.get("e") == "error":
            logger.error("WebSocket error: %s", msg)
            return

        kline = msg.get("k", {})
        symbol = msg.get("s", "")
        is_closed = kline.get("x", False)
        current_price = float(kline.get("c", 0))

        if is_closed:
            logger.info("[%s] 4H candle closed at %.4f", symbol, current_price)
            try:
                portfolio.run_signal_cycle()
                portfolio.run_exit_cycle(symbol, current_price)
                save_state(state)
            except Exception as exc:
                logger.error("[%s] Signal/exit cycle error: %s", symbol, exc, exc_info=True)
        else:
            # Intracandle: update trailing stops with current price
            try:
                portfolio.run_exit_cycle(symbol, current_price)
            except Exception as exc:
                logger.debug("[%s] Exit cycle update error: %s", symbol, exc)

    # Subscribe to 4H kline streams for all symbols
    stream_keys = []
    for symbol in SYMBOLS:
        try:
            key = twm.start_futures_kline_socket(
                callback=on_kline_message,
                symbol=symbol.lower(),
                interval="4h",
            )
            stream_keys.append(key)
            logger.info("[%s] WebSocket subscribed (4H klines)", symbol)
        except Exception as exc:
            logger.error("[%s] Failed to subscribe to WebSocket: %s", symbol, exc)

    # ---------------------------------------------------------------------------
    # Main loop
    # ---------------------------------------------------------------------------
    logger.info("Strategy running. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(60)
            # Log periodic status
            equity = portfolio.get_account_equity()
            open_pos = list(state.get("open_positions", {}).keys())
            logger.debug(
                "Status: equity=$%.2f | open=%s | regime=%s",
                equity, open_pos or "none", state.get("btc_regime", "UNKNOWN"),
            )
    except KeyboardInterrupt:
        logger.info("Shutdown requested by user...")
    finally:
        try:
            twm.stop()
        except Exception:
            pass
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass
        save_state(state)
        logger.info("Strategy stopped cleanly.")


if __name__ == "__main__":
    main()
