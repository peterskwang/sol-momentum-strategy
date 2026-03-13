# SOL Regime-Filtered ATR Momentum Strategy

A systematic long-only momentum strategy trading SOLUSDT, ETHUSDT, and AVAXUSDT perpetual futures on Binance.  
Entry is gated by a BTC macro regime filter (EMA20 > EMA50 = BULL), with Donchian channel breakout, volume confirmation, and ATR-based position sizing. Risk is fixed at 1% of equity per trade, inversely weighted by volatility across pairs.

---

## Strategy Overview

The strategy enters long positions when:
1. **BTC Macro Regime = BULL** (BTC daily EMA20 > EMA50) — checked daily at 00:05 UTC
2. **Donchian Breakout** — 4H close > highest high of prior 20 candles
3. **Bullish Candle** — breakout candle is green (close > open)
4. **Volume Confirmation** — breakout candle volume > 1.5× 20-period average

Position sizing uses inverse-volatility weighting: `risk_dollars / (1.5 × ATR14)`.  
Exits: fixed stop at 1.5× ATR, TP1 at 2.0× ATR (close 50%), then trailing stop at 1.0× ATR below high watermark.  
If funding rate < -0.01% (shorts paying longs), position size is boosted by 20%.

---

## Architecture

```
┌────────────────────────────────────────────────────────┐
│                     run_live.py                        │
│              (Main entry point + scheduler)            │
└────┬───────────────────────────────────────────────────┘
     │
     ├── strategy/regime_filter.py   ← BTC EMA regime (daily)
     ├── strategy/signal_generator.py ← 4H Donchian + volume + ATR
     ├── strategy/funding_rate.py    ← Funding rate boost
     ├── strategy/position_sizer.py  ← ATR-based sizing
     ├── strategy/exit_manager.py    ← Stop / TP1 / trailing stop
     ├── strategy/portfolio.py       ← Multi-pair orchestration
     │
     ├── execution/binance_client.py ← Client factory + retry
     ├── execution/order_manager.py  ← Order placement (paper/live)
     │
     ├── state_manager.py            ← Atomic JSON state persistence
     └── notifications.py            ← Telegram alerts
```

---

## Setup

```bash
# 1. Clone the repository
git clone https://github.com/peterskwang/sol-momentum-strategy.git
cd sol-momentum-strategy

# 2. Install dependencies
pip install -r requirements.txt

# 3. Copy and configure environment
cp .env.example .env
# Edit .env and fill in your API keys and optional Telegram credentials
```

---

## Environment Variables

Copy `.env.example` to `.env` and set the following:

| Variable | Required | Description |
|---|---|---|
| `BINANCE_API_KEY` | ✅ | Your Binance Futures API key |
| `BINANCE_API_SECRET` | ✅ | Your Binance Futures API secret |
| `BINANCE_TESTNET` | No | Set `true` to use Binance testnet (default: `false`) |
| `LIVE_TRADING` | No | Set `true` to enable real orders (default: `false` = paper mode) |
| `TELEGRAM_BOT_TOKEN` | No | Telegram bot token for alerts |
| `TELEGRAM_CHAT_ID` | No | Telegram chat ID to receive alerts |

---

## Running the Strategy

### Paper Mode (default — no real orders)

```bash
python run_live.py
```

Paper mode is active by default. No real orders will be placed. The strategy simulates trades using current mark prices.

### Live Trading (real orders)

⚠️ **Both** the environment variable AND the CLI flag are required (belt and suspenders):

```bash
LIVE_TRADING=true python run_live.py --live
```

### Binance Testnet

```bash
BINANCE_TESTNET=true python run_live.py --testnet
```

### Additional CLI Options

```bash
python run_live.py --log-level DEBUG   # verbose logging
```

---

## Running the Backtest

Fetches 2023-2025 historical data from Binance (cached to `backtest/data/`):

```bash
python run_backtest.py
```

Custom date range and equity:

```bash
python run_backtest.py --start 2023-01-01 --end 2025-12-31 --equity 50000
```

**Expected backtest targets:**
- Sharpe ratio: 1.0–1.6
- Max drawdown: 12–25%
- Win rate: 40–55%
- Each pair: ≥20 trades

---

## Running Tests

```bash
pytest tests/ -v
```

All 15+ unit tests should pass. Coverage includes:
- Donchian breakout (true, false, edge cases)
- Bullish candle detection
- Volume confirmation
- ATR Wilder smoothing
- BTC regime (BULL/BEAR)
- BEAR regime blocking signals
- Position sizing formula
- Leverage cap enforcement
- Funding rate boost
- Portfolio risk cap
- Paper mode (no real API calls)

---

## Risk Disclosure

⚠️ **Trading perpetual futures involves substantial risk of loss. This software is provided for educational and research purposes only. Past backtest performance does not guarantee future results. Use paper mode until you fully understand the strategy. Never risk money you cannot afford to lose. The authors accept no responsibility for financial losses.**

- Default leverage: 2×
- Max leverage: 3×
- Paper mode is the default; live trading requires explicit opt-in
- All API keys are loaded from environment variables; never hardcode credentials

---

## Project Structure

```
sol-momentum-strategy/
├── run_live.py              # Main entry point
├── run_backtest.py          # Backtest runner
├── config.py                # Central configuration
├── state_manager.py         # Atomic state persistence
├── notifications.py         # Telegram alerts
├── requirements.txt
├── .env.example
│
├── strategy/
│   ├── regime_filter.py     # BTC EMA regime filter
│   ├── signal_generator.py  # 4H entry signals
│   ├── funding_rate.py      # Funding rate boost
│   ├── position_sizer.py    # ATR-based sizing
│   ├── exit_manager.py      # Stop/TP/trailing logic
│   └── portfolio.py         # Multi-pair orchestration
│
├── execution/
│   ├── binance_client.py    # Client factory + retry
│   └── order_manager.py     # Order placement
│
├── state/
│   └── strategy_state.json  # Live state (auto-created)
│
├── backtest/
│   ├── backtest_sol.py      # Core backtest engine
│   └── data/                # Cached CSV data (gitignored)
│
├── logs/                    # Log files (gitignored)
└── tests/
    └── test_signals.py      # Unit tests
```
