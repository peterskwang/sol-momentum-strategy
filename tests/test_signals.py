"""
tests/test_signals.py — Unit tests for signal generator, regime filter,
position sizer, funding rate, and portfolio risk management.

Run with: pytest tests/ -v
"""

import os
import pytest
import numpy as np
import pandas as pd
from unittest.mock import MagicMock, patch, call
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Helper: synthetic OHLCV DataFrame
# ---------------------------------------------------------------------------

def make_ohlcv(n: int = 50, base_close: float = 100.0, trend: float = 0.0) -> pd.DataFrame:
    """Create a synthetic OHLCV DataFrame with n candles."""
    np.random.seed(42)
    closes = [base_close + i * trend + np.random.uniform(-2, 2) for i in range(n)]
    highs = [c + np.random.uniform(0.5, 2.5) for c in closes]
    lows = [c - np.random.uniform(0.5, 2.5) for c in closes]
    opens = [c + np.random.uniform(-1.5, 1.5) for c in closes]
    volumes = [np.random.uniform(800, 1200) for _ in range(n)]

    timestamps = pd.date_range("2024-01-01", periods=n, freq="4h", tz="UTC")
    df = pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    }, index=timestamps)
    return df


def make_btc_daily(n: int = 70, base: float = 40000.0, trend: str = "bull") -> pd.DataFrame:
    """Create synthetic BTC daily data with a clear bull or bear trend."""
    if trend == "bull":
        slope = 200.0   # rising: EMA20 will cross above EMA50
    else:
        slope = -200.0  # falling: EMA20 will cross below EMA50

    closes = [base + i * slope for i in range(n)]
    timestamps = pd.date_range("2024-01-01", periods=n, freq="1D", tz="UTC")
    df = pd.DataFrame({
        "open": closes,
        "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes],
        "close": closes,
        "volume": [1e9] * n,
    }, index=timestamps)
    return df


# ===========================================================================
# Donchian Breakout Tests
# ===========================================================================

class TestDonchianBreakout:

    def test_donchian_breakout_true(self):
        """Confirm breakout detected when close > 20-period prior high."""
        from strategy.signal_generator import check_donchian_breakout

        df = make_ohlcv(n=50, base_close=100.0)
        # Force the last candle's close to be definitively above all prior highs
        df.iloc[-1, df.columns.get_loc("close")] = 999.0
        df.iloc[-1, df.columns.get_loc("high")] = 1000.0

        assert check_donchian_breakout(df) is True

    def test_donchian_breakout_false_equal(self):
        """Confirm no breakout when close == 20-period prior high (strict >)."""
        from strategy.signal_generator import check_donchian_breakout

        df = make_ohlcv(n=50, base_close=100.0)
        # Set all prior highs to exactly 105.0
        for i in range(-21, -1):
            df.iloc[i, df.columns.get_loc("high")] = 105.0
        # Set current close exactly at 105.0 (not strictly greater)
        df.iloc[-1, df.columns.get_loc("close")] = 105.0

        assert check_donchian_breakout(df) is False

    def test_donchian_breakout_insufficient_data(self):
        """Confirm ValueError when fewer than 22 candles provided."""
        from strategy.signal_generator import check_donchian_breakout

        df = make_ohlcv(n=21)  # need 22 minimum (period=20 → 20+2=22)
        with pytest.raises(ValueError):
            check_donchian_breakout(df)

    def test_donchian_breakout_below_upper_band(self):
        """No breakout when close is below the 20-period upper band."""
        from strategy.signal_generator import check_donchian_breakout

        df = make_ohlcv(n=50, base_close=100.0)
        # Force prior highs to be very high
        for i in range(-21, -1):
            df.iloc[i, df.columns.get_loc("high")] = 200.0
        # Set current close below the upper band
        df.iloc[-1, df.columns.get_loc("close")] = 150.0

        assert check_donchian_breakout(df) is False


# ===========================================================================
# Bullish Candle Tests
# ===========================================================================

class TestBullishCandle:

    def test_bullish_candle_true(self):
        """close > open → True."""
        from strategy.signal_generator import check_bullish_candle

        df = make_ohlcv(n=5)
        df.iloc[-1, df.columns.get_loc("open")] = 100.0
        df.iloc[-1, df.columns.get_loc("close")] = 105.0
        assert check_bullish_candle(df) is True

    def test_bullish_candle_false(self):
        """close <= open → False."""
        from strategy.signal_generator import check_bullish_candle

        df = make_ohlcv(n=5)
        df.iloc[-1, df.columns.get_loc("open")] = 100.0
        df.iloc[-1, df.columns.get_loc("close")] = 99.0
        assert check_bullish_candle(df) is False

    def test_bullish_candle_equal(self):
        """close == open → False (doji not bullish)."""
        from strategy.signal_generator import check_bullish_candle

        df = make_ohlcv(n=5)
        df.iloc[-1, df.columns.get_loc("open")] = 100.0
        df.iloc[-1, df.columns.get_loc("close")] = 100.0
        assert check_bullish_candle(df) is False


# ===========================================================================
# Volume Confirmation Tests
# ===========================================================================

class TestVolumeConfirmation:

    def test_volume_confirmation_true(self):
        """Volume > 1.5× avg → True."""
        from strategy.signal_generator import check_volume_confirmation

        df = make_ohlcv(n=50)
        # Set prior 20 volumes all to 1000
        for i in range(-21, -1):
            df.iloc[i, df.columns.get_loc("volume")] = 1000.0
        # Set current volume well above 1.5x
        df.iloc[-1, df.columns.get_loc("volume")] = 1600.0  # > 1500 (1.5 × 1000)

        assert check_volume_confirmation(df) is True

    def test_volume_confirmation_false(self):
        """Volume == 1.5× avg → False (strict >)."""
        from strategy.signal_generator import check_volume_confirmation

        df = make_ohlcv(n=50)
        for i in range(-21, -1):
            df.iloc[i, df.columns.get_loc("volume")] = 1000.0
        # Set current volume exactly at 1.5× avg (not strictly greater)
        df.iloc[-1, df.columns.get_loc("volume")] = 1500.0  # == 1.5 × 1000, not >

        assert check_volume_confirmation(df) is False

    def test_volume_confirmation_below_threshold(self):
        """Volume below 1.5× avg → False."""
        from strategy.signal_generator import check_volume_confirmation

        df = make_ohlcv(n=50)
        for i in range(-21, -1):
            df.iloc[i, df.columns.get_loc("volume")] = 1000.0
        df.iloc[-1, df.columns.get_loc("volume")] = 1200.0  # < 1500

        assert check_volume_confirmation(df) is False


# ===========================================================================
# ATR Tests
# ===========================================================================

class TestATRCalculation:

    def test_atr_wilder_smoothing(self):
        """Verify ATR(14) is positive and matches expected magnitude for synthetic data."""
        from strategy.signal_generator import compute_atr14

        # Create data with constant range of 2.0 (high-low=2, no gaps)
        n = 50
        closes = [100.0] * n
        highs = [101.0] * n
        lows = [99.0] * n

        timestamps = pd.date_range("2024-01-01", periods=n, freq="4h", tz="UTC")
        df = pd.DataFrame({
            "open": closes,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [1000.0] * n,
        }, index=timestamps)

        atr = compute_atr14(df)

        # With constant range of 2.0, ATR should be very close to 2.0
        assert 1.5 <= atr <= 2.5, f"ATR={atr:.4f} outside expected range [1.5, 2.5]"

    def test_atr_insufficient_data(self):
        """ATR raises ValueError with insufficient data."""
        from strategy.signal_generator import compute_atr14

        df = make_ohlcv(n=20)  # need at least 30 (ATR_WARMUP_CANDLES)
        with pytest.raises(ValueError):
            compute_atr14(df)

    def test_atr_positive(self):
        """ATR should always be positive for normal market data."""
        from strategy.signal_generator import compute_atr14

        df = make_ohlcv(n=50, base_close=150.0, trend=1.0)
        atr = compute_atr14(df)
        assert atr > 0


# ===========================================================================
# Regime Filter Tests
# ===========================================================================

class TestRegimeFilter:

    def test_regime_bull(self):
        """EMA20 > EMA50 → BULL."""
        from strategy.regime_filter import compute_regime

        df = make_btc_daily(n=70, base=40000.0, trend="bull")
        regime = compute_regime(df)
        assert regime == "BULL"

    def test_regime_bear(self):
        """EMA20 < EMA50 → BEAR."""
        from strategy.regime_filter import compute_regime

        df = make_btc_daily(n=70, base=40000.0, trend="bear")
        regime = compute_regime(df)
        assert regime == "BEAR"

    def test_bear_regime_blocks_signal(self):
        """generate_entry_signal returns signal=False in BEAR regime regardless of other conditions."""
        from strategy.signal_generator import generate_entry_signal

        mock_client = MagicMock()
        result = generate_entry_signal(mock_client, "SOLUSDT", "BEAR")

        assert result["signal"] is False
        assert "BEAR" in result["reason"]
        # Client should not be called for klines when regime blocks immediately
        mock_client.futures_klines.assert_not_called()

    def test_regime_ema_values(self):
        """Verify EMA20 vs EMA50 calculation logic directly."""
        from strategy.regime_filter import compute_regime

        # Decreasing prices: EMA20 reacts faster, will be lower than EMA50
        closes = [100.0 - i * 1.5 for i in range(70)]
        timestamps = pd.date_range("2024-01-01", periods=70, freq="1D", tz="UTC")
        df = pd.DataFrame({"close": closes}, index=timestamps)

        regime = compute_regime(df)
        assert regime == "BEAR"


# ===========================================================================
# Position Sizer Tests
# ===========================================================================

class TestPositionSizer:

    def test_position_size_formula(self):
        """Verify position size calculation matches manual computation."""
        from strategy.position_sizer import compute_position_size

        equity = 10000.0
        atr = 5.0
        price = 150.0
        symbol = "SOLUSDT"

        # Manual: risk_dollars = 10000 * 0.01 * 0.50 = 50.0
        # stop_distance = 1.5 * 5.0 = 7.5
        # quantity = 50.0 / 7.5 = 6.666...
        # notional = 6.666... * 150 = 1000
        # leverage = 1000 / 10000 = 0.1x (well under 3x)

        result = compute_position_size(
            account_equity=equity,
            symbol=symbol,
            atr14=atr,
            current_price=price,
            funding_boost=1.0,
        )

        expected_qty = 50.0 / 7.5  # 6.666...
        assert abs(result["quantity"] - expected_qty) < 0.01
        assert abs(result["stop_loss"] - (price - 1.5 * atr)) < 0.001
        assert abs(result["tp1_price"] - (price + 2.0 * atr)) < 0.001
        assert abs(result["risk_dollars"] - 50.0) < 0.001

    def test_position_size_leverage_cap(self):
        """Verify leverage is capped at MAX_LEVERAGE (3×)."""
        from strategy.position_sizer import compute_position_size

        # Use very small equity + large position to force leverage > 3
        equity = 100.0
        atr = 0.001   # tiny ATR → huge position
        price = 150.0
        symbol = "SOLUSDT"

        result = compute_position_size(
            account_equity=equity,
            symbol=symbol,
            atr14=atr,
            current_price=price,
            funding_boost=1.0,
            max_leverage=3.0,
        )

        assert result["capped"] is True
        assert result["leverage"] <= 3.0 + 1e-9

    def test_position_size_funding_boost(self):
        """Funding boost of 1.2 increases position size by 20%."""
        from strategy.position_sizer import compute_position_size

        equity = 10000.0
        atr = 5.0
        price = 150.0

        result_normal = compute_position_size(
            account_equity=equity,
            symbol="SOLUSDT",
            atr14=atr,
            current_price=price,
            funding_boost=1.0,
        )

        result_boosted = compute_position_size(
            account_equity=equity,
            symbol="SOLUSDT",
            atr14=atr,
            current_price=price,
            funding_boost=1.2,
        )

        # Boosted risk_dollars should be 20% higher
        assert abs(result_boosted["risk_dollars"] / result_normal["risk_dollars"] - 1.2) < 0.001

    def test_position_size_invalid_atr(self):
        """ValueError raised when ATR is zero or negative."""
        from strategy.position_sizer import compute_position_size

        with pytest.raises(ValueError):
            compute_position_size(
                account_equity=10000.0,
                symbol="SOLUSDT",
                atr14=0.0,
                current_price=150.0,
            )

    def test_round_step_size(self):
        """Round step size floors correctly."""
        from strategy.position_sizer import round_step_size

        assert abs(round_step_size(6.777, 0.1) - 6.7) < 1e-9
        assert abs(round_step_size(10.999, 1.0) - 10.0) < 1e-9
        assert abs(round_step_size(0.0056, 0.001) - 0.005) < 1e-9


# ===========================================================================
# Funding Rate Tests
# ===========================================================================

class TestFundingRate:

    def test_funding_boost_negative(self):
        """Funding rate < -0.0001 → boost = 1.20."""
        from strategy.funding_rate import get_funding_boost

        boost = get_funding_boost(-0.0002)
        assert abs(boost - 1.20) < 1e-9

    def test_funding_boost_neutral(self):
        """Funding rate >= -0.0001 → boost = 1.00."""
        from strategy.funding_rate import get_funding_boost

        assert abs(get_funding_boost(0.0001) - 1.0) < 1e-9
        assert abs(get_funding_boost(-0.0001) - 1.0) < 1e-9
        assert abs(get_funding_boost(0.0) - 1.0) < 1e-9

    def test_funding_boost_threshold_exact(self):
        """Exact threshold: -0.0001 is NOT below threshold (strict <)."""
        from strategy.funding_rate import get_funding_boost

        # threshold is -0.0001; funding == threshold → no boost
        boost = get_funding_boost(-0.0001, threshold=-0.0001)
        assert abs(boost - 1.0) < 1e-9

    def test_fetch_current_funding_rate(self):
        """Mock Binance client returns correct funding rate."""
        from strategy.funding_rate import fetch_current_funding_rate

        mock_client = MagicMock()
        mock_client.futures_mark_price.return_value = {
            "symbol": "SOLUSDT",
            "markPrice": "150.00",
            "lastFundingRate": "-0.000150",
        }

        rate = fetch_current_funding_rate(mock_client, "SOLUSDT")
        assert abs(rate - (-0.000150)) < 1e-9


# ===========================================================================
# Portfolio Risk Cap Tests
# ===========================================================================

class TestPortfolioRiskCap:

    def _make_portfolio(self, open_positions=None, paper_equity=10000.0):
        """Helper to create a Portfolio with mock state."""
        from strategy.portfolio import Portfolio

        mock_client = MagicMock()
        state = {
            "open_positions": open_positions or {},
            "paper_equity": paper_equity,
            "paper_pnl": 0.0,
            "btc_regime": "BULL",
        }
        config = {
            "live_trading": False,
            "paper_initial_equity": 10000.0,
        }
        return Portfolio(client=mock_client, state=state, config=config)

    def test_portfolio_risk_cap(self):
        """New position rejected if total risk would exceed 2%."""
        from config import MAX_PORTFOLIO_RISK_PCT

        # Simulate existing open position eating 1.8% of equity
        equity = 10000.0
        # risk = (entry - stop) * qty = (150 - 135) * 12 = 180 = 1.8% of 10000
        portfolio = self._make_portfolio(
            open_positions={
                "SOLUSDT": {
                    "entry_price": 150.0,
                    "stop_price": 135.0,
                    "quantity_remaining": 12.0,
                    "quantity_total": 12.0,
                }
            },
            paper_equity=equity,
        )

        # Proposed new trade would add 0.5% risk (50 dollars)
        allowed, reason = portfolio.can_open_position(
            symbol="ETHUSDT",
            proposed_risk_dollars=50.0,
            proposed_notional=500.0,
        )

        # 1.8% + 0.5% = 2.3% > 2.0% → rejected
        assert allowed is False
        assert "cap" in reason.lower() or "risk" in reason.lower()

    def test_portfolio_allows_within_cap(self):
        """New position allowed if total risk stays under 2%."""
        portfolio = self._make_portfolio(paper_equity=10000.0)

        allowed, reason = portfolio.can_open_position(
            symbol="SOLUSDT",
            proposed_risk_dollars=50.0,   # 0.5% of 10000
            proposed_notional=500.0,
        )

        assert allowed is True

    def test_portfolio_rejects_duplicate(self):
        """New position rejected if symbol already in open positions."""
        portfolio = self._make_portfolio(
            open_positions={
                "SOLUSDT": {
                    "entry_price": 150.0,
                    "stop_price": 140.0,
                    "quantity_remaining": 5.0,
                    "quantity_total": 5.0,
                }
            }
        )

        allowed, reason = portfolio.can_open_position(
            symbol="SOLUSDT",
            proposed_risk_dollars=10.0,
            proposed_notional=100.0,
        )

        assert allowed is False
        assert "position" in reason.lower() or "SOLUSDT" in reason


# ===========================================================================
# Paper Mode Tests
# ===========================================================================

class TestPaperMode:

    def test_paper_mode_no_api_calls(self, mocker):
        """Verify no Binance order API calls made in paper mode."""
        import os
        # Ensure paper mode is active
        mocker.patch.dict(os.environ, {"LIVE_TRADING": "false"})

        from execution.order_manager import place_market_order

        mock_client = MagicMock()
        # Provide a mark price response for paper fill
        mock_client.futures_mark_price.return_value = {
            "symbol": "SOLUSDT",
            "markPrice": "150.00",
            "lastFundingRate": "0.0001",
        }

        result = place_market_order(
            client=mock_client,
            symbol="SOLUSDT",
            side="BUY",
            quantity=5.0,
        )

        # futures_create_order should NOT be called in paper mode
        mock_client.futures_create_order.assert_not_called()
        assert result["paper"] is True
        assert result["status"] == "FILLED"

    def test_paper_stop_loss_no_api(self, mocker):
        """Stop loss order in paper mode: no API call, returns mock response."""
        import os
        mocker.patch.dict(os.environ, {"LIVE_TRADING": "false"})

        from execution.order_manager import place_stop_loss_order

        mock_client = MagicMock()
        result = place_stop_loss_order(mock_client, "SOLUSDT", 5.0, 140.0)

        mock_client.futures_create_order.assert_not_called()
        assert result["paper"] is True
        assert result["type"] == "STOP_MARKET"


# ===========================================================================
# Exit Manager Tests
# ===========================================================================

class TestExitManager:

    def test_compute_initial_exits(self):
        """Verify initial exit prices computed correctly."""
        from strategy.exit_manager import compute_initial_exits

        exits = compute_initial_exits(entry_price=150.0, atr14=5.0)

        assert abs(exits["stop_price"] - (150.0 - 1.5 * 5.0)) < 1e-9  # 142.5
        assert abs(exits["tp1_price"] - (150.0 + 2.0 * 5.0)) < 1e-9   # 160.0
        assert exits["trail_atr"] == 5.0

    def test_trailing_stop_not_active_before_tp1(self):
        """Trailing stop not updated when tp1_hit is False."""
        from strategy.exit_manager import TradeState, update_trailing_stop

        trade = TradeState(
            symbol="SOLUSDT",
            entry_price=150.0,
            quantity_total=10.0,
            quantity_remaining=10.0,
            stop_price=142.5,
            tp1_price=160.0,
            tp1_hit=False,
            trail_atr=5.0,
            trail_high_watermark=0.0,
            trail_stop_price=0.0,
        )

        updated = update_trailing_stop(trade, current_price=155.0)
        assert updated.trail_high_watermark == 0.0  # unchanged

    def test_trailing_stop_updates_watermark(self):
        """Trailing stop watermark updated when tp1_hit is True."""
        from strategy.exit_manager import TradeState, update_trailing_stop

        trade = TradeState(
            symbol="SOLUSDT",
            entry_price=150.0,
            quantity_total=10.0,
            quantity_remaining=5.0,
            stop_price=142.5,
            tp1_price=160.0,
            tp1_hit=True,
            trail_atr=5.0,
            trail_high_watermark=162.0,
            trail_stop_price=157.0,
        )

        # New price is higher than watermark
        updated = update_trailing_stop(trade, current_price=165.0)
        assert updated.trail_high_watermark == 165.0
        assert abs(updated.trail_stop_price - (165.0 - 1.0 * 5.0)) < 1e-9  # 160.0
