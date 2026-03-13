"""Order placement helpers."""
from __future__ import annotations

import logging
from typing import Any

from execution.binance_client import api_call_with_retry
from utils.telegram import (
    send_trade_closed_alert,
    send_trade_open_alert,
    send_tp1_hit_alert,
)


def _paper_order_response(**kwargs: Any) -> dict[str, Any]:
    return {"paper": True, **kwargs}


def place_market_order(client: Any, symbol: str, side: str, quantity: float, paper_mode: bool = True) -> dict[str, Any]:
    if paper_mode:
        return _paper_order_response(symbol=symbol, side=side, quantity=quantity, status="FILLED")

    def _call() -> Any:
        return client.futures_create_order(symbol=symbol, side=side, type="MARKET", quantity=quantity)

    return api_call_with_retry(_call)


def place_stop_loss_order(client: Any, symbol: str, side: str, stop_price: float, quantity: float, paper_mode: bool = True) -> dict[str, Any]:
    if paper_mode:
        return _paper_order_response(symbol=symbol, side=side, stop_price=stop_price, quantity=quantity, type="STOP_MARKET")

    def _call() -> Any:
        return client.futures_create_order(
            symbol=symbol,
            side=side,
            type="STOP_MARKET",
            stopPrice=stop_price,
            closePosition=True,
            reduceOnly=True,
        )

    return api_call_with_retry(_call)


def place_limit_tp_order(client: Any, symbol: str, side: str, price: float, quantity: float, paper_mode: bool = True) -> dict[str, Any]:
    if paper_mode:
        return _paper_order_response(symbol=symbol, side=side, price=price, quantity=quantity, type="LIMIT")

    def _call() -> Any:
        return client.futures_create_order(
            symbol=symbol,
            side=side,
            type="LIMIT",
            price=price,
            quantity=quantity,
            timeInForce="GTC",
            reduceOnly=True,
        )

    return api_call_with_retry(_call)


def place_trailing_stop_order(
    client: Any,
    symbol: str,
    side: str,
    callback_rate: float,
    quantity: float,
    paper_mode: bool = True,
) -> dict[str, Any]:
    callback_rate = max(0.1, min(5.0, callback_rate))
    if paper_mode:
        return _paper_order_response(
            symbol=symbol,
            side=side,
            callbackRate=callback_rate,
            quantity=quantity,
            type="TRAILING_STOP_MARKET",
        )

    def _call() -> Any:
        return client.futures_create_order(
            symbol=symbol,
            side=side,
            type="TRAILING_STOP_MARKET",
            callbackRate=callback_rate,
            quantity=quantity,
            reduceOnly=True,
        )

    return api_call_with_retry(_call)


def cancel_order(client: Any, symbol: str, order_id: str, paper_mode: bool = True) -> dict[str, Any]:
    if paper_mode:
        return _paper_order_response(symbol=symbol, orderId=order_id, status="CANCELED")

    def _call() -> Any:
        return client.futures_cancel_order(symbol=symbol, orderId=order_id)

    return api_call_with_retry(_call)


class OrderManager:
    """Manages order placement with live_trading injected at construction."""

    def __init__(self, client: Any, live_trading: bool = False) -> None:
        self.client = client
        self.live_trading = live_trading

    @property
    def _paper(self) -> bool:
        return not self.live_trading

    def place_market_order(self, symbol: str, side: str, quantity: float) -> dict[str, Any]:
        return place_market_order(self.client, symbol=symbol, side=side, quantity=quantity, paper_mode=self._paper)

    def place_stop_loss_order(self, symbol: str, side: str, stop_price: float, quantity: float) -> dict[str, Any]:
        return place_stop_loss_order(
            self.client,
            symbol=symbol,
            side=side,
            stop_price=stop_price,
            quantity=quantity,
            paper_mode=self._paper,
        )

    def place_limit_tp_order(self, symbol: str, side: str, price: float, quantity: float) -> dict[str, Any]:
        return place_limit_tp_order(
            self.client,
            symbol=symbol,
            side=side,
            price=price,
            quantity=quantity,
            paper_mode=self._paper,
        )

    def place_trailing_stop_order(self, symbol: str, side: str, callback_rate: float, quantity: float) -> dict[str, Any]:
        return place_trailing_stop_order(
            self.client,
            symbol=symbol,
            side=side,
            callback_rate=callback_rate,
            quantity=quantity,
            paper_mode=self._paper,
        )

    def cancel_order(self, symbol: str, order_id: str) -> dict[str, Any]:
        return cancel_order(self.client, symbol=symbol, order_id=order_id, paper_mode=self._paper)


def notify_trade_open(
    *,
    symbol: str,
    entry_price: float,
    qty: float,
    notional: float,
    stop_price: float,
    risk_dollars: float,
    tp1_price: float,
    regime: str,
    funding_rate: float,
    funding_boost: float,
    logger: logging.Logger | None = None,
) -> bool:
    """Send the standardized trade-open Telegram alert."""

    return send_trade_open_alert(
        symbol=symbol,
        price=entry_price,
        qty=qty,
        notional=notional,
        stop=stop_price,
        risk=risk_dollars,
        tp1=tp1_price,
        regime=regime,
        funding_rate=funding_rate,
        funding_boost=funding_boost,
        logger=logger,
    )


def notify_tp1_hit(
    *,
    symbol: str,
    tp1_price: float,
    realized_pnl: float,
    remaining_qty: float,
    base_asset: str | None = None,
    logger: logging.Logger | None = None,
) -> bool:
    """Send the TP1 fill alert."""

    return send_tp1_hit_alert(
        symbol=symbol,
        tp1=tp1_price,
        pnl=realized_pnl,
        remaining_qty=remaining_qty,
        base_asset=base_asset,
        logger=logger,
    )


def notify_trade_closed(
    *,
    symbol: str,
    exit_price: float,
    reason: str,
    realized_pnl: float,
    pct_return: float,
    hold_hours: float,
    logger: logging.Logger | None = None,
) -> bool:
    """Send the trade closed alert."""

    return send_trade_closed_alert(
        symbol=symbol,
        price=exit_price,
        reason=reason,
        pnl=realized_pnl,
        pct=pct_return,
        hold_hours=hold_hours,
        logger=logger,
    )
