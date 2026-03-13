"""Configuration and constant definitions for the SOL momentum strategy."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List

from dotenv import load_dotenv

# Strategy constants
SYMBOLS: List[str] = ["SOLUSDT", "ETHUSDT", "AVAXUSDT"]
BTC_SYMBOL = "BTCUSDT"
BTC_EMA_FAST = 20
BTC_EMA_SLOW = 50
DONCHIAN_PERIOD = 20
VOLUME_MULTIPLIER = 1.5
SIGNAL_INTERVAL = "4h"
FUNDING_BOOST_THRESHOLD = -0.0001
FUNDING_BOOST_FACTOR = 1.20
STOP_ATR_MULT = 1.5
TP1_ATR_MULT = 2.0
TRAIL_ATR_MULT = 1.0
TP1_CLOSE_PCT = 0.5
RISK_PCT = 0.01
PAIR_WEIGHTS: Dict[str, float] = {
    "SOLUSDT": 0.50,
    "ETHUSDT": 0.30,
    "AVAXUSDT": 0.20,
}
MAX_LEVERAGE = 3.0
MAX_PORTFOLIO_RISK_PCT = 0.02
MAX_PORTFOLIO_NOTIONAL_X = 3.0
ATR_PERIOD = 14


@dataclass
class Config:
    """Runtime configuration loaded from the environment."""

    api_key: str | None
    api_secret: str | None
    testnet: bool
    live_trading: bool
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    log_level: str


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_config() -> Config:
    """Load environment configuration values.

    Returns:
        Config: Fully-populated configuration dataclass.
    """

    load_dotenv()

    return Config(
        api_key=os.getenv("BINANCE_API_KEY"),
        api_secret=os.getenv("BINANCE_API_SECRET"),
        testnet=_as_bool(os.getenv("BINANCE_TESTNET"), default=False),
        live_trading=_as_bool(os.getenv("LIVE_TRADING"), default=False),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )
