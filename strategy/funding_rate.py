"""Funding rate helpers."""
from __future__ import annotations

from typing import Any

from config import FUNDING_BOOST_FACTOR, FUNDING_BOOST_THRESHOLD


def fetch_current_funding_rate(client: Any, symbol: str) -> float:
    """Fetch the current funding rate for a symbol."""

    resp = client.futures_funding_rate(symbol=symbol, limit=1)
    if not resp:
        raise ValueError("No funding rate data returned")
    return float(resp[0]["fundingRate"])


def get_funding_boost(rate: float) -> float:
    """Return the funding boost multiplier based on rate."""

    if rate <= FUNDING_BOOST_THRESHOLD:
        return FUNDING_BOOST_FACTOR
    return 1.0
