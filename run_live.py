"""Live execution entry point for the SOL regime-filtered momentum strategy."""
from __future__ import annotations

import argparse
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

from apscheduler.schedulers.background import BackgroundScheduler
from binance import ThreadedWebsocketManager
from websocket import WebSocketApp

from config import MAX_LEVERAGE, SYMBOLS, load_config
from execution.binance_client import create_client, setup_futures_leverage
from strategy.portfolio import Portfolio
from strategy.regime_filter import update_regime
from utils.state import save_state
from utils.telegram import (
    send_error_alert,
    send_startup_alert,
    send_websocket_fallback_alert,
)

STATE_PATH = Path("state/strategy_state.json")
STREAMS = ["solusdt@kline_4h", "ethusdt@kline_4h", "avaxusdt@kline_4h"]


class WebSocketController:
    """Manage Binance kline WebSocket connectivity with reconnect/fallback logic."""

    def __init__(
        self,
        portfolio: Portfolio,
        regime_getter: Callable[[], str],
        logger: logging.Logger,
        fallback_callback: Callable[[], None],
    ) -> None:
        self.portfolio = portfolio
        self.regime_getter = regime_getter
        self.logger = logger
        self.fallback_callback = fallback_callback
        self._thread: threading.Thread | None = None
        self._ws_app: WebSocketApp | None = None
        self._stop_event = threading.Event()
        self._force_refresh = threading.Event()
        self._fallback_triggered = False
        self._reconnect_attempts = 0
        self._max_attempts = 5

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._ws_app:
            self._ws_app.close()
        if self._thread:
            self._thread.join(timeout=5)

    def request_refresh(self) -> None:
        if self._ws_app:
            self._force_refresh.set()
            self._ws_app.close()

    def _run_loop(self) -> None:
        while not self._stop_event.is_set() and not self._fallback_triggered:
            self._ws_app = self._create_app()
            self._ws_app.run_forever()

            if self._stop_event.is_set():
                break

            if self._force_refresh.is_set():
                self.logger.info("WebSocket proactive refresh completed")
                self._force_refresh.clear()
                self._reconnect_attempts = 0
                continue

            self._reconnect_attempts += 1
            if self._reconnect_attempts > self._max_attempts:
                self.logger.error("Max WebSocket reconnect attempts exceeded; enabling REST fallback")
                self._fallback_triggered = True
                self.fallback_callback()
                send_websocket_fallback_alert(logger=self.logger)
                send_error_alert(
                    component="WebSocket",
                    error="Failed to reconnect after 5 attempts",
                    action="Switching to REST polling (4h)",
                    logger=self.logger,
                )
                break

            delay = min(16, 2 ** (self._reconnect_attempts - 1))
            self.logger.warning(
                "WebSocket reconnect attempt %s/%s in %ss",
                self._reconnect_attempts,
                self._max_attempts,
                delay,
            )
            time.sleep(delay)

    def _create_app(self) -> WebSocketApp:
        url = f"wss://fstream.binance.com/stream?streams={'/'.join(STREAMS)}"

        def on_message(_: Any, message: str) -> None:
            try:
                data = json.loads(message)
                candle = data.get("data", {}).get("k", {})
                if candle.get("x"):
                    regime = self.regime_getter()
                    self.portfolio.run_signal_cycle(regime)
                    self.portfolio.run_exit_cycle()
            except Exception as exc:  # noqa: BLE001
                self.logger.exception("WebSocket message handling failed: %s", exc)

        def on_open(_: Any) -> None:
            self.logger.info("WebSocket connected")
            self._reconnect_attempts = 0

        def on_error(_: Any, error: Exception) -> None:
            self.logger.error("WebSocket error: %s", error)
            send_error_alert(
                component="WebSocket",
                error=str(error),
                action="Retrying connection",
                logger=self.logger,
            )

        def on_close(_: Any, status_code: int, msg: str) -> None:
            self.logger.warning("WebSocket closed (%s, %s)", status_code, msg)

        return WebSocketApp(
            url,
            on_message=on_message,
            on_open=on_open,
            on_error=on_error,
            on_close=on_close,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run SOL regime momentum live loop")
    parser.add_argument("--live", action="store_true", help="Enable live trading (requires env flag)")
    parser.add_argument("--testnet", action="store_true", help="Use Binance Futures testnet")
    parser.add_argument("--log-level", default="INFO", help="Logging level")
    return parser.parse_args()


def ensure_state_file() -> None:
    if not STATE_PATH.exists():
        save_state(
            {
                "version": "1.0",
                "btc_regime": "BULL",
                "open_positions": {},
                "closed_trades": [],
                "paper_mode": True,
                "paper_equity": 10000.0,
                "paper_pnl": 0.0,
                "lot_size_cache": {},
            },
            STATE_PATH,
        )


def start_user_data_stream(
    portfolio: Portfolio,
    twm: ThreadedWebsocketManager,
    logger: logging.Logger,
) -> None:
    """Subscribe to Binance Futures user data stream to receive fill events."""

    def on_user_data_message(msg: dict[str, Any]) -> None:
        event_type = msg.get("e")
        if event_type != "ORDER_TRADE_UPDATE":
            return
        order_status = msg.get("o", {})
        if order_status.get("X") != "FILLED":
            return

        symbol = order_status.get("s")
        order_id = order_status.get("i")
        order_type = order_status.get("o")
        fill_price = float(order_status.get("ap", 0) or 0)

        logger.info(
            "User data fill event: symbol=%s order_id=%s type=%s fill_price=%.4f",
            symbol,
            order_id,
            order_type,
            fill_price,
        )
        try:
            portfolio.handle_fill_event(
                symbol=symbol,
                order_id=order_id,
                order_type=order_type,
                fill_price=fill_price,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("handle_fill_event error for %s: %s", symbol, exc)

    twm.start_futures_user_socket(callback=on_user_data_message)
    logger.info("User data stream subscribed")


def main() -> None:
    args = parse_args()
    cfg = load_config()

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))
    logger = logging.getLogger("run_live")

    live_enabled = cfg.live_trading and args.live
    if live_enabled and (not cfg.api_key or not cfg.api_secret):
        raise RuntimeError("Live trading requires API credentials")

    mode_label = "LIVE" if live_enabled else "PAPER"
    logger.info("Starting strategy in %s mode", mode_label)

    ensure_state_file()
    send_startup_alert(mode_label, logger=logger)

    client = create_client(cfg.api_key, cfg.api_secret, testnet=args.testnet)

    for symbol in SYMBOLS:
        setup_futures_leverage(client, symbol, int(MAX_LEVERAGE), logger=logger)

    portfolio = Portfolio(
        client=client,
        state_path=STATE_PATH,
        logger=logger,
        live_trading=live_enabled,
    )

    def refresh_regime() -> str:
        state = portfolio.state
        regime = update_regime(client, state, logger=logger)
        portfolio._save_state()
        return regime

    refresh_regime()

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(refresh_regime, "cron", hour=0, minute=5, id="regime")
    scheduler.add_job(lambda: logger.info("Keepalive"), "interval", minutes=30, id="keepalive")

    rest_polling_enabled = False

    def run_rest_cycle() -> None:
        regime = portfolio.state.get("btc_regime", "BULL")
        portfolio.run_signal_cycle(regime)
        portfolio.run_exit_cycle()

    def enable_rest_polling() -> None:
        nonlocal rest_polling_enabled
        if rest_polling_enabled:
            return
        scheduler.add_job(run_rest_cycle, "interval", hours=4, id="rest_fallback", replace_existing=True)
        rest_polling_enabled = True
        run_rest_cycle()

    ws_controller = WebSocketController(
        portfolio=portfolio,
        regime_getter=lambda: portfolio.state.get("btc_regime", "BULL"),
        logger=logger,
        fallback_callback=enable_rest_polling,
    )
    ws_controller.start()

    scheduler.add_job(ws_controller.request_refresh, "interval", hours=23, minutes=50, id="ws_refresh")
    scheduler.start()

    twm: ThreadedWebsocketManager | None = None
    if cfg.api_key and cfg.api_secret:
        twm = ThreadedWebsocketManager(api_key=cfg.api_key, api_secret=cfg.api_secret, testnet=args.testnet)
        twm.start()
        start_user_data_stream(portfolio, twm, logger)
    else:
        logger.warning("Skipping user data stream subscription (missing API credentials)")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        ws_controller.stop()
        scheduler.shutdown(wait=False)
        if twm:
            twm.stop()


if __name__ == "__main__":
    main()
