# SOL Regime-Filtered ATR Momentum Strategy

Apex Quant's SOL momentum engine trades SOLUSDT, ETHUSDT, and AVAXUSDT perpetual futures on Binance. Entries require a BTC macro regime tailwind plus Donchian, bullish candle, and volume confirmation. Position sizing, exits, and funding adjustments are all ATR-driven.

## Strategy Overview

1. **BTC Macro Regime** – Daily EMA20 vs EMA50 crossover keeps us long-only when BTC structure is bullish.
2. **4h Donchian Breakout** – Close above the highest high of the prior 20 candles.
3. **Bullish Candle Check** – Breakout candle must close above the open and prior close.
4. **Volume Confirmation** – Breakout candle volume ≥ 1.5× the recent average.
5. **ATR Risk Framework** – 1% portfolio risk per trade, weighted by pair weights, stop at 1.5× ATR, TP1 at 2× ATR (close 50%), trailing stop at 1× ATR.
6. **Funding Boost** – If funding ≤ -0.01%, position size is boosted 20%.

## Architecture

```
+-----------------------------+
|         run_live.py         |
|  CLI + schedulers + WS      |
+---------------+-------------+
                |
                v
+---------------+-------------+
|        strategy/portfolio    |
| (signal + sizing + exits)    |
+------+-------+---------------+
       |       |
       |       +--> execution/ (Binance client + order manager)
       |
       +--> strategy/
             |- regime_filter.py
             |- signal_generator.py
             |- funding_rate.py
             |- position_sizer.py
             |- exit_manager.py
```

## Setup

```bash
git clone https://github.com/peterskwang/sol-momentum-strategy.git
cd sol-momentum-strategy
pip install -r requirements.txt
cp .env.example .env  # fill in credentials if needed
```

### Environment Variables

| Variable | Required | Description |
| --- | --- | --- |
| `BINANCE_API_KEY` | For live | Binance Futures API key |
| `BINANCE_API_SECRET` | For live | Binance Futures API secret |
| `BINANCE_TESTNET` | Optional | `true` routes to testnet (default `false`) |
| `LIVE_TRADING` | Optional | Must be `true` **and** `--live` flag to send real orders |
| `TELEGRAM_BOT_TOKEN` | Optional | For alerting (placeholder) |
| `TELEGRAM_CHAT_ID` | Optional | Telegram chat target |

## Running

### Paper mode (default)

```bash
python run_live.py --log-level INFO
```

### Live trading

Requires BOTH the environment flag and CLI flag:

```bash
LIVE_TRADING=true BINANCE_API_KEY=... BINANCE_API_SECRET=... python run_live.py --live
```

### Binance testnet

```bash
BINANCE_TESTNET=true python run_live.py --testnet
```

The live runner loads state, refreshes the BTC regime (00:05 UTC), spins up a Binance Futures websocket for SOL/ETH/AVAX 4h klines, and processes signal/exit cycles on each closed candle. APScheduler also emits 30-minute keep-alive logs.

## Backtest

```bash
python run_backtest.py
```

This downloads (and caches) Binance 4h klines for 2023‑01‑01 through 2025‑12‑31 and simulates fills with a 4 bps taker fee. Summary metrics include trades, win rate, Sharpe, max drawdown, total return, profit factor, and per-symbol stats.

## Testing

```bash
pytest tests/ -v
```

Unit tests cover Donchian logic, candle/volume gates, ATR smoothing, funding boosts, position sizing, portfolio risk caps, and paper-mode order handling.

## Risk Disclosure

Perpetual futures are highly leveraged instruments. Improper sizing or infrastructure failures can lead to losses exceeding account equity. This repository is for research; use at your own risk and validate every assumption before deploying real capital.
