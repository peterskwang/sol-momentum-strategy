"""Portfolio orchestration."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from config import (
    ATR_PERIOD,
    MAX_PORTFOLIO_NOTIONAL_X,
    MAX_PORTFOLIO_RISK_PCT,
    STOP_ATR_MULT,
    SYMBOLS,
    TP1_CLOSE_PCT,
    TRAIL_ATR_MULT,
)
from execution.order_manager import OrderManager, notify_trade_open
from strategy.exit_manager import (
    TradeState,
    compute_initial_exits,
    handle_stop_fill,
    handle_tp1_fill,
)
from strategy.funding_rate import fetch_current_funding_rate, get_funding_boost
from strategy.position_sizer import compute_position_size, get_symbol_lot_size
from strategy.signal_generator import compute_atr, fetch_4h_klines, generate_entry_signal
from utils.state import save_state


class Portfolio:
    def __init__(
        self,
        client: Any,
        state_path: Path,
        logger: logging.Logger | None = None,
        live_trading: bool = False,
    ) -> None:
        self.client = client
        self.state_path = state_path
        self.logger = logger or logging.getLogger(__name__)
        self.live_trading = live_trading
        self.order_manager = OrderManager(client=self.client, live_trading=self.live_trading)
        self.state = self._load_state()

    # ------------------ state helpers ------------------
    def _load_state(self) -> Dict[str, Any]:
        if not self.state_path.exists():
            raise FileNotFoundError(self.state_path)
        state = json.loads(self.state_path.read_text())
        for trade in state.get("open_positions", {}).values():
            if "tp1_filled" in trade and "tp1_hit" not in trade:
                trade["tp1_hit"] = trade.pop("tp1_filled")
            trade.setdefault("trail_high_watermark", None)
            trade.setdefault("opened_at", datetime.now(timezone.utc).isoformat())
        return state

    def _save_state(self) -> None:
        save_state(self.state, self.state_path)

    # ------------------ metrics ------------------
    def get_account_equity(self) -> float:
        if not self.live_trading:
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

    # ------------------ fill event handler ------------------
    def handle_fill_event(
        self,
        symbol: str,
        order_id: int,
        order_type: str,
        fill_price: float,
    ) -> None:
        positions = self.state.get("open_positions", {})
        trade_dict = positions.get(symbol)
        if trade_dict is None:
            return

        tp1_order_id = trade_dict.get("tp1_order_id")
        stop_order_id = trade_dict.get("stop_order_id")

        if tp1_order_id is not None and order_id == tp1_order_id:
            trade = self._dict_to_trade_state(symbol, trade_dict)
            handle_tp1_fill(trade, fill_price, logger=self.logger)
            trade_dict.update(trade.__dict__)
            self.logger.info(
                "TP1 fill event: %s @ %.4f — tp1_hit=True, remaining_qty=%.4f",
                symbol,
                fill_price,
                trade_dict["remaining_qty"],
            )
            if stop_order_id and self.live_trading:
                try:
                    self.order_manager.cancel_order(symbol, str(stop_order_id))
                except Exception as exc:  # noqa: BLE001
                    self.logger.warning("Could not cancel stop order for %s: %s", symbol, exc)
            if trade_dict["remaining_qty"] > 0:
                trailing_resp = self.order_manager.place_trailing_stop_order(
                    symbol=symbol,
                    side="SELL",
                    callback_rate=TRAIL_ATR_MULT * 100 / max(trade_dict["entry_price"], 1),
                    quantity=trade_dict["remaining_qty"],
                )
                trade_dict["trailing_order_id"] = trailing_resp.get("orderId")
            self._save_state()

        elif stop_order_id is not None and order_id == stop_order_id:
            trade = self._dict_to_trade_state(symbol, trade_dict)
            handle_stop_fill(trade, fill_price, reason=order_type or "STOP", logger=self.logger)
            trade_dict.update(trade.__dict__)
            self.logger.info(
                "Stop fill event: %s @ %.4f — trade closed via Binance STOP_MARKET",
                symbol,
                fill_price,
            )
            self._close_position(symbol, trade_dict)

    # ------------------ position close ------------------
    def _close_position(self, symbol: str, trade_dict: Dict[str, Any]) -> None:
        trade_dict["closed"] = True
        positions = self.state.get("open_positions", {})
        positions.pop(symbol, None)

        closed = self.state.setdefault("closed_trades", [])
        closed.append(trade_dict)

        if not self.live_trading:
            realized = trade_dict.get("realized_pnl", 0.0)
            self.state["paper_equity"] = self.state.get("paper_equity", 0.0) + realized
            self.state["paper_pnl"] = self.state.get("paper_pnl", 0.0) + realized

        self._save_state()
        self.logger.info(
            "Position closed: %s  realized_pnl=%.2f  remaining_qty=%.4f",
            symbol,
            trade_dict.get("realized_pnl", 0.0),
            trade_dict.get("remaining_qty", 0.0),
        )

    # ------------------ internal helpers ------------------
    @staticmethod
    def _dict_to_trade_state(symbol: str, d: Dict[str, Any]) -> TradeState:
        opened_at = d.get("opened_at") or datetime.now(timezone.utc).isoformat()
        return TradeState(
            symbol=symbol,
            entry_price=d["entry_price"],
            atr=d["atr"],
            qty=d["qty"],
            stop_price=d["stop_price"],
            tp1_price=d["tp1_price"],
            trailing_stop=d["trailing_stop"],
            remaining_qty=d["remaining_qty"],
            realized_pnl=d.get("realized_pnl", 0.0),
            tp1_hit=d.get("tp1_hit", d.get("tp1_filled", False)),
            closed=d.get("closed", False),
            trail_high_watermark=d.get("trail_high_watermark"),
            opened_at=opened_at,
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

                funding_rate_raw = fetch_current_funding_rate(self.client, symbol)
                funding_rate = float(funding_rate_raw or 0.0)
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

                response = self.order_manager.place_market_order(
                    symbol=symbol,
                    side="BUY",
                    quantity=qty,
                )
                self.logger.info("Entry order response: %s", response)

                trade = compute_initial_exits(symbol, price, atr, qty)
                trade_dict = trade.__dict__
                trade_dict["risk_dollars"] = risk_dollars
                trade_dict["notional"] = notional
                trade_dict["funding_boost"] = funding_boost
                trade_dict["funding_rate"] = funding_rate
                trade_dict["regime_at_entry"] = btc_regime

                tp1_qty = qty * TP1_CLOSE_PCT
                tp1_resp = self.order_manager.place_limit_tp_order(
                    symbol=symbol,
                    side="SELL",
                    price=trade.tp1_price,
                    quantity=tp1_qty,
                )
                trade_dict["tp1_order_id"] = tp1_resp.get("orderId")

                stop_resp = self.order_manager.place_stop_loss_order(
                    symbol=symbol,
                    side="SELL",
                    stop_price=trade.stop_price,
                    quantity=qty,
                )
                trade_dict["stop_order_id"] = stop_resp.get("orderId")

                self.state.setdefault("open_positions", {})[symbol] = trade_dict
                self._save_state()

                notify_trade_open(
                    symbol=symbol,
                    entry_price=price,
                    qty=qty,
                    notional=notional,
                    stop_price=trade.stop_price,
                    risk_dollars=risk_dollars,
                    tp1_price=trade.tp1_price,
                    regime=btc_regime,
                    funding_rate=funding_rate,
                    funding_boost=funding_boost,
                    logger=self.logger,
                )
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
