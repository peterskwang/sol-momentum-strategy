"""
execution/binance_client.py — Binance Client Factory & Retry Utilities

Wraps python-binance Client with retry logic, testnet support, and
rate limit handling.
"""

import os
import time
import logging
from typing import Callable, Any

from binance.client import Client
from binance.exceptions import BinanceAPIException
import requests

logger = logging.getLogger(__name__)


def create_client(testnet: bool = False) -> Client:
    """
    Create and return authenticated Binance Client.

    Args:
        testnet: if True, use testnet endpoint

    Returns:
        Authenticated Binance Client instance

    Raises:
        EnvironmentError: if API keys are not set
        ConnectionError: if connectivity check fails
    """
    api_key = os.environ.get("BINANCE_API_KEY", "")
    api_secret = os.environ.get("BINANCE_API_SECRET", "")

    if not api_key:
        raise EnvironmentError(
            "BINANCE_API_KEY is not set. Please set it in your environment or .env file."
        )
    if not api_secret:
        raise EnvironmentError(
            "BINANCE_API_SECRET is not set. Please set it in your environment or .env file."
        )

    client = Client(
        api_key=api_key,
        api_secret=api_secret,
        testnet=testnet,
        requests_params={"timeout": 10},
    )

    # Verify connectivity
    try:
        client.futures_ping()
        logger.info(
            "Binance Futures client connected (%s)",
            "TESTNET" if testnet else "MAINNET",
        )
    except Exception as exc:
        raise ConnectionError(
            f"Failed to connect to Binance Futures API: {exc}"
        ) from exc

    return client


def api_call_with_retry(
    func: Callable,
    *args,
    max_retries: int = 3,
    backoff_s: float = 5.0,
    **kwargs,
) -> Any:
    """
    Execute a Binance API call with exponential backoff retry.

    Retries on:
        - BinanceAPIException with status 429 (rate limit) — respect Retry-After
        - BinanceAPIException with status 5xx (server error)
        - requests.exceptions.ConnectionError / TimeoutError

    Does NOT retry on:
        - 400 Bad Request (malformed params)
        - 401 Unauthorized (invalid key)

    Returns:
        API response

    Raises:
        BinanceAPIException after max_retries exhausted
    """
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            return func(*args, **kwargs)

        except BinanceAPIException as exc:
            last_error = exc
            status_code = exc.status_code if hasattr(exc, "status_code") else 0

            if status_code == 429:
                retry_after = int(exc.response.headers.get("Retry-After", int(backoff_s))) if hasattr(exc, "response") and exc.response else int(backoff_s)
                logger.warning(
                    "Rate limit hit (429). Waiting %ds before retry (attempt %d/%d).",
                    retry_after, attempt, max_retries,
                )
                time.sleep(retry_after)

            elif 500 <= status_code < 600:
                wait = backoff_s * (2 ** (attempt - 1))
                logger.warning(
                    "Server error %d. Waiting %.1fs before retry (attempt %d/%d).",
                    status_code, wait, attempt, max_retries,
                )
                time.sleep(wait)

            else:
                # Non-retriable error (400, 401, etc.)
                logger.error(
                    "Non-retriable Binance API error %d: %s",
                    status_code, exc,
                )
                raise

        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_error = exc
            wait = backoff_s * (2 ** (attempt - 1))
            logger.warning(
                "Network error. Waiting %.1fs before retry (attempt %d/%d): %s",
                wait, attempt, max_retries, exc,
            )
            time.sleep(wait)

    logger.error(
        "All %d retry attempts failed. Last error: %s",
        max_retries, last_error,
    )
    raise last_error


def setup_futures_leverage(client: Client, symbol: str, leverage: int) -> None:
    """
    Set leverage for a symbol using POST /fapi/v1/leverage.

    Args:
        client: Authenticated Binance Client
        symbol: e.g. "SOLUSDT"
        leverage: integer (2 or 3)
    """
    try:
        resp = client.futures_change_leverage(symbol=symbol, leverage=leverage)
        logger.info(
            "[%s] Leverage set to %d× (maxNotionalValue: %s)",
            symbol, leverage, resp.get("maxNotionalValue", "N/A"),
        )
    except BinanceAPIException as exc:
        logger.error("[%s] Failed to set leverage to %d×: %s", symbol, leverage, exc)
        raise
