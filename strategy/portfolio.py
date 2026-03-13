"""Portfolio orchestration."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

from config import ATR_PERIOD, MAX_PORTFOLIO_RISK_PCT, MAX_PORTFOLIO_NOTIONAL_X, STOP_ATR_MULT, SYMBOLS
from execution.order_manager import place_market_order
from strategy.exit_manager import compute_initial_exits
from strategy.funding_rate import fetch_current_funding_rate, get_funding_boost
from strategy.position_sizer import compute_position_size, get_symbol_lot_size
from strategy.signal_generator import compute_atr, fetch_4h_klines, generate_entry_signal


class Portfolio:
    def __init__(
        self,
        client: Any,
        state_path: Path,
        logger: logging.Logger | None = None,
        paper_mode: bool = True,
    ) -> None:
        self.client = client
        self.state_path = state_path
        self.logger = logger or logging.getLogger(__name__)
        self.paper_mode = paper_mode
        self.state = self._load_state()

    # ------------------ state helpers ------------------
    def _load_state(self) -> Dict[str, Any]:
        if not self.state_path.exists():
            raise FileNotFoundError(self.state_path)
        return json.loads(self.state_path.read_text())

    def _save_state(self) -> None:
        self.state_path.write_text(json.dumps(self.state, indent=2))

    # ------------------ metrics ------------------
    def get_account_equity(self) -> float:
        if self.paper_mode:
            return float(self.state.get("paper_equity", 0))
        account = self.client.futures_account_balance()
        usdt = next((float(x["balance"]) for x in account if x["asset"] == "USDT"), 0.0)
        return usdt

    def get_total_open_risk(self) -> float:
        total = 0.0
        for trade in self.state.get("open_positions", {}).values():
            total += trade.get("risk_dollars", 0.0)
        return total

    def get_total_open_notional(self) -> float:
        total = 0.0
        for trade in self.state.get("open_positions", {}).values():
            total += trade.get("notional", 0.0)
        return total

    def can_open_position(self, equity: float, new_risk: float, new_notional: float) -> bool:
        max_risk = equity * MAX_PORTFOLIO_RISK_PCT
        max_notional = equity * MAX_PORTFOLIO_NOTIONAL_X
        return (
            (self.get_total_open_risk() + new_risk) <= max_risk
            and (self.get_total_open_notional() + new_notional) <= max_notional
        )

    # ------------------ trading cycles ------------------
    def run_signal_cycle(self, btc_regime: str) -> None:
        cache = self.state.setdefault("lot_size_cache", {})
        equity = self.get_account_equity()

        for symbol in SYMBOLS:
            try:
                df = fetch_4h_klines(self.client, symbol)
                signal = generate_entry_signal(symbol, df, btc_regime)
                if not signal:
                    continue

                atr_series = compute_atr(df, ATR_PERIOD)
                atr = float(atr_series.iloc[-1])
                price = float(df["close"].astype(float).iloc[-1])

                funding_rate = fetch_current_funding_rate(self.client, symbol)
                funding_boost = get_funding_boost(funding_rate)

                lot_step = get_symbol_lot_size(self.client, symbol, cache)
                qty = compute_position_size(
                    symbol=symbol,
                    equity=equity,
                    price=price,
                    atr14=atr,
                    funding_boost=funding_boost,
                    lot_step=lot_step,
                )
                if qty <= 0:
                    continue

                risk_dollars = qty * atr * STOP_ATR_MULT
                notional = qty * price
                if not self.can_open_position(equity, risk_dollars, notional):
                    continue

                response = place_market_order(
                    self.client,
                    symbol=symbol,
                    side="BUY",
                    quantity=qty,
                    paper_mode=self.paper_mode,
                )
                self.logger.info("Order response: %s", response)

                trade = compute_initial_exits(symbol, price, atr, qty)
                trade_dict = trade.__dict__
                trade_dict["risk_dollars"] = risk_dollars
                trade_dict["notional"] = notional
                trade_dict["funding_boost"] = funding_boost
                self.state.setdefault("open_positions", {})[symbol] = trade_dict
                self._save_state()
            except Exception as exc:  # noqa: BLE001
                self.logger.error("Signal cycle failed for %s: %s", symbol, exc)

    def run_exit_cycle(self) -> None:
        positions = self.state.get("open_positions", {})
        to_remove = []
        for symbol, trade in positions.items():
            if trade.get("closed"):
                to_remove.append(symbol)
        for symbol in to_remove:
            positions.pop(symbol, None)
        if to_remove:
            self._save_state()
