"""
config.py — Central configuration for SOL Regime-Filtered ATR Momentum Strategy.

All strategy parameters. Loads overrides from environment variables.
"""

import os
import logging
from dotenv import load_dotenv

# Load .env file if present (also checks secrets/ directory)
load_dotenv()
load_dotenv("secrets/binance_sol_strategy.env")

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Symbols
# ---------------------------------------------------------------------------
SYMBOLS = ["SOLUSDT", "ETHUSDT", "AVAXUSDT"]
BTC_SYMBOL = "BTCUSDT"

# ---------------------------------------------------------------------------
# Regime filter
# ---------------------------------------------------------------------------
BTC_EMA_FAST = 20          # EMA period for bull/bear detection
BTC_EMA_SLOW = 50
REGIME_CHECK_HOUR = 0      # UTC hour for daily regime update
REGIME_CHECK_MINUTE = 5    # UTC minute for daily regime update

# ---------------------------------------------------------------------------
# Entry signal
# ---------------------------------------------------------------------------
DONCHIAN_PERIOD = 20       # number of prior completed candles for upper band
VOLUME_MULTIPLIER = 1.5    # breakout candle volume > 1.5x avg volume
SIGNAL_INTERVAL = "4h"     # 4H timeframe

# ---------------------------------------------------------------------------
# Funding rate
# ---------------------------------------------------------------------------
FUNDING_BOOST_THRESHOLD = -0.0001   # -0.01% = -0.0001 as fraction
FUNDING_BOOST_FACTOR = 1.20         # +20% size boost

# ---------------------------------------------------------------------------
# Exit parameters
# ---------------------------------------------------------------------------
STOP_ATR_MULT = 1.5        # stop = entry - STOP_ATR_MULT × ATR14
TP1_ATR_MULT = 2.0         # TP1 = entry + TP1_ATR_MULT × ATR14
TRAIL_ATR_MULT = 1.0       # trailing stop = 1x ATR14 below high watermark
TP1_CLOSE_PCT = 0.5        # close 50% at TP1

# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------
RISK_PCT = 0.01            # 1% of equity risk per trade
PAIR_WEIGHTS = {
    "SOLUSDT": 0.50,
    "ETHUSDT": 0.30,
    "AVAXUSDT": 0.20,
}
MAX_LEVERAGE = 3.0         # hard cap
MIN_LEVERAGE = 2.0

# ---------------------------------------------------------------------------
# Portfolio risk caps
# ---------------------------------------------------------------------------
MAX_PORTFOLIO_RISK_PCT = 0.02   # 2% total open risk cap
MAX_PORTFOLIO_NOTIONAL_X = 3.0  # 3× equity notional cap

# ---------------------------------------------------------------------------
# ATR
# ---------------------------------------------------------------------------
ATR_PERIOD = 14
ATR_WARMUP_CANDLES = 30    # minimum candles needed before ATR is stable

# ---------------------------------------------------------------------------
# API settings
# ---------------------------------------------------------------------------
REST_BASE_URL = "https://fapi.binance.com"
TESTNET_BASE_URL = "https://testnet.binancefuture.com"
WS_BASE_URL = "wss://fstream.binance.com"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_FILE = "logs/trading.log"
STATE_FILE = "state/strategy_state.json"

# ---------------------------------------------------------------------------
# Paper trading defaults
# ---------------------------------------------------------------------------
PAPER_INITIAL_EQUITY = 10000.0


# ---------------------------------------------------------------------------
# Runtime config loader
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """
    Load configuration from environment variables.
    Overrides defaults if env vars set.
    Returns config dict with all parameters.

    Environment variables:
    - BINANCE_API_KEY, BINANCE_API_SECRET (required for live/paper)
    - BINANCE_TESTNET (bool string, default "false")
    - LIVE_TRADING (bool string, default "false")
    - TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (for alerts)
    """
    api_key = os.environ.get("BINANCE_API_KEY", "")
    api_secret = os.environ.get("BINANCE_API_SECRET", "")

    testnet = os.environ.get("BINANCE_TESTNET", "false").lower() == "true"
    live_trading = os.environ.get("LIVE_TRADING", "false").lower() == "true"

    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    return {
        # API
        "api_key": api_key,
        "api_secret": api_secret,
        "testnet": testnet,
        "live_trading": live_trading,
        # Telegram
        "telegram_bot_token": telegram_token,
        "telegram_chat_id": telegram_chat_id,
        # Strategy
        "symbols": SYMBOLS,
        "btc_symbol": BTC_SYMBOL,
        "btc_ema_fast": BTC_EMA_FAST,
        "btc_ema_slow": BTC_EMA_SLOW,
        "regime_check_hour": REGIME_CHECK_HOUR,
        "regime_check_minute": REGIME_CHECK_MINUTE,
        "donchian_period": DONCHIAN_PERIOD,
        "volume_multiplier": VOLUME_MULTIPLIER,
        "signal_interval": SIGNAL_INTERVAL,
        "funding_boost_threshold": FUNDING_BOOST_THRESHOLD,
        "funding_boost_factor": FUNDING_BOOST_FACTOR,
        "stop_atr_mult": STOP_ATR_MULT,
        "tp1_atr_mult": TP1_ATR_MULT,
        "trail_atr_mult": TRAIL_ATR_MULT,
        "tp1_close_pct": TP1_CLOSE_PCT,
        "risk_pct": RISK_PCT,
        "pair_weights": PAIR_WEIGHTS,
        "max_leverage": MAX_LEVERAGE,
        "min_leverage": MIN_LEVERAGE,
        "max_portfolio_risk_pct": MAX_PORTFOLIO_RISK_PCT,
        "max_portfolio_notional_x": MAX_PORTFOLIO_NOTIONAL_X,
        "atr_period": ATR_PERIOD,
        "atr_warmup_candles": ATR_WARMUP_CANDLES,
        "log_file": LOG_FILE,
        "state_file": STATE_FILE,
        "paper_initial_equity": PAPER_INITIAL_EQUITY,
    }


def validate_config(config: dict, require_keys: bool = True) -> None:
    """
    Validate configuration. Raises EnvironmentError if API keys are missing
    and require_keys is True.
    """
    if require_keys:
        if not config.get("api_key"):
            raise EnvironmentError(
                "BINANCE_API_KEY is not set. Please set it in your environment or .env file."
            )
        if not config.get("api_secret"):
            raise EnvironmentError(
                "BINANCE_API_SECRET is not set. Please set it in your environment or .env file."
            )
