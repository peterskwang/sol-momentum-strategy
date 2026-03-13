import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))

from strategy.signal_generator import (
    check_bullish_candle,
    check_donchian_breakout,
    check_volume_confirmation,
    compute_atr,
    generate_entry_signal,
)
from strategy.regime_filter import REGIME_BEAR, compute_regime
from strategy.position_sizer import compute_position_size
from strategy.funding_rate import get_funding_boost
from strategy.portfolio import Portfolio
from execution.order_manager import place_market_order


@pytest.fixture
def sample_df():
    data = {
        "open": [10, 11, 12, 13, 14, 15],
        "high": [11, 12, 13, 14, 15, 16],
        "low": [9, 10, 11, 12, 13, 14],
        "close": [10.5, 11.5, 12.5, 13.5, 14.5, 15.5],
        "volume": [100, 110, 120, 130, 140, 300],
    }
    return pd.DataFrame(data)


def test_donchian_breakout_true(sample_df):
    highs = sample_df["high"]
    closes = sample_df["close"]
    assert check_donchian_breakout(highs, closes, period=3)


def test_donchian_breakout_false_equal(sample_df):
    highs = sample_df["high"]
    closes = sample_df["close"].copy()
    closes.iloc[-1] = highs.iloc[-2]
    assert not check_donchian_breakout(highs, closes, period=3)


def test_donchian_breakout_insufficient_data(sample_df):
    with pytest.raises(ValueError):
        check_donchian_breakout(sample_df["high"].tail(1), sample_df["close"].tail(1), period=3)


def test_bullish_candle_true(sample_df):
    assert check_bullish_candle(sample_df["open"], sample_df["close"])


def test_bullish_candle_false(sample_df):
    closes = sample_df["close"].copy()
    closes.iloc[-1] = sample_df["open"].iloc[-1] - 1
    assert not check_bullish_candle(sample_df["open"], closes)


def test_volume_confirmation_true(sample_df):
    assert check_volume_confirmation(sample_df["volume"], multiplier=1.5)


def test_volume_confirmation_false(sample_df):
    volumes = sample_df["volume"].copy()
    volumes.iloc[-1] = 50
    assert not check_volume_confirmation(volumes, multiplier=1.5)


def test_atr_wilder_smoothing(sample_df):
    atr = compute_atr(sample_df, period=3)
    assert pytest.approx(atr.iloc[-1], rel=1e-3) == atr.iloc[-1]


def test_regime_bull():
    data = {"close": [100, 101, 102, 103, 104]}
    df = pd.DataFrame(data)
    assert compute_regime(df) == "BULL"


def test_regime_bear():
    data = {"close": [110, 109, 108, 107, 106]}
    df = pd.DataFrame(data)
    assert compute_regime(df) == "BEAR"


def test_bear_regime_blocks_signal(sample_df):
    assert not generate_entry_signal("SOLUSDT", sample_df, REGIME_BEAR)


def test_position_size_formula():
    qty = compute_position_size(
        symbol="SOLUSDT",
        equity=10000,
        price=150,
        atr14=2,
        funding_boost=1.0,
        lot_step=0.1,
        pair_weights={"SOLUSDT": 1.0},
        risk_pct=0.01,
        stop_atr_mult=1.5,
    )
    assert qty == pytest.approx(33.3, rel=1e-3)


def test_position_size_leverage_cap():
    qty = compute_position_size(
        symbol="SOLUSDT",
        equity=10000,
        price=1000,
        atr14=1,
        funding_boost=1.0,
        lot_step=0.01,
        pair_weights={"SOLUSDT": 1.0},
        risk_pct=0.05,
        stop_atr_mult=1.0,
        max_leverage=3.0,
    )
    assert qty <= 30


def test_funding_boost_negative():
    assert get_funding_boost(-0.001) > 1


def test_funding_boost_neutral():
    assert get_funding_boost(0.0) == 1.0


def test_portfolio_risk_cap(tmp_path):
    state = {
        "version": "1.0",
        "btc_regime": "BULL",
        "open_positions": {"SOLUSDT": {"risk_dollars": 150, "notional": 5000}},
        "paper_equity": 10000,
    }
    path = tmp_path / "state.json"
    path.write_text(json.dumps(state))

    portfolio = Portfolio(client=SimpleNamespace(), state_path=path, paper_mode=True)
    assert not portfolio.can_open_position(10000, 1000, 30000)  # exceeds cap


def test_paper_mode_no_api_calls():
    class Dummy:
        def __init__(self):
            self.called = False

        def futures_create_order(self, *args, **kwargs):  # pragma: no cover
            self.called = True
            return {"status": "FILLED"}

    dummy = Dummy()
    response = place_market_order(dummy, "SOLUSDT", "BUY", 1.0, paper_mode=True)
    assert response["paper"] is True
    assert not dummy.called
