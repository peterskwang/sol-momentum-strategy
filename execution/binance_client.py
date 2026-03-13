"""Binance client helpers."""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

from binance.client import Client
from binance.exceptions import BinanceAPIException


def create_client(api_key: str | None, api_secret: str | None, testnet: bool = False) -> Client:
    client = Client(api_key, api_secret, testnet=testnet)
    client.FUTURES_URL = client.FUTURES_TESTNET_URL if testnet else client.FUTURES_URL
    return client


def api_call_with_retry(func: Callable[[], Any], max_retries: int = 3, backoff: float = 1.0, logger: logging.Logger | None = None) -> Any:
    attempt = 0
    while True:
        try:
            return func()
        except BinanceAPIException as exc:
            attempt += 1
            should_retry = exc.status_code in {429, 500, 502, 503}
            if not should_retry or attempt > max_retries:
                raise
            sleep_for = backoff * attempt
            if logger:
                logger.warning("Binance API call failed (%s). Retrying in %.1fs", exc.status_code, sleep_for)
            time.sleep(sleep_for)


def setup_futures_leverage(client: Client, symbol: str, leverage: int, logger: logging.Logger | None = None) -> None:
    def _call() -> Any:
        return client.futures_change_leverage(symbol=symbol, leverage=leverage)

    api_call_with_retry(_call, logger=logger)
    if logger:
        logger.info("Set %s leverage to %s", symbol, leverage)
