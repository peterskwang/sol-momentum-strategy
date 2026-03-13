"""
strategy/funding_rate.py — Funding Rate Boost Logic

Fetches current funding rate from Binance Futures premiumIndex endpoint.
Computes position size multiplier if funding rate is sufficiently negative
(shorts paying longs → boost long position size by 20%).
"""

import logging

from config import FUNDING_BOOST_THRESHOLD, FUNDING_BOOST_FACTOR

logger = logging.getLogger(__name__)


def fetch_current_funding_rate(client, symbol: str) -> float:
    """
    Fetch the latest funding rate for a symbol.

    Args:
        client: Binance Client
        symbol: e.g. "SOLUSDT"

    Returns:
        Funding rate as float (e.g. 0.0003 = 0.03%, -0.0001 = -0.01%)
        The returned value is already a decimal fraction (not percentage).

    Raises:
        BinanceAPIException on error
    """
    data = client.futures_mark_price(symbol=symbol)

    # futures_mark_price returns a single dict when symbol is specified
    if isinstance(data, list):
        data = next((d for d in data if d.get("symbol") == symbol), data[0])

    funding_rate = float(data.get("lastFundingRate", 0.0))
    logger.debug("[%s] Current funding rate: %.6f (%.4f%%)", symbol, funding_rate, funding_rate * 100)
    return funding_rate


def get_funding_boost(
    funding_rate: float,
    threshold: float = FUNDING_BOOST_THRESHOLD,
) -> float:
    """
    Compute position size multiplier based on funding rate.

    Args:
        funding_rate: current funding rate as float
        threshold: boost trigger threshold (default -0.0001 = -0.01%)

    Returns:
        1.20 if funding_rate < threshold (shorts paying longs → boost long)
        1.00 otherwise

    Notes:
        - Boost is capped; cannot push leverage above MAX_LEVERAGE
        - Leverage cap enforcement is done in position_sizer.py
    """
    if funding_rate < threshold:
        logger.debug(
            "Funding boost ACTIVE: rate=%.6f < threshold=%.6f → boost=%.2f",
            funding_rate, threshold, FUNDING_BOOST_FACTOR,
        )
        return FUNDING_BOOST_FACTOR
    return 1.0
