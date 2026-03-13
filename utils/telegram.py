"""Telegram alert helpers for the SOL momentum strategy."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage" if TELEGRAM_BOT_TOKEN else None


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _base_asset(symbol: str) -> str:
    if symbol.upper().endswith("USDT"):
        return symbol[:-4]
    return symbol


def _format_usd(value: float) -> str:
    return f"${value:,.2f}"


def _send_message(payload: dict[str, Any], logger: logging.Logger | None = None) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or not _API_URL:
        if logger:
            logger.debug("Telegram not configured; skipping alert")
        return False

    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": payload.get("text", ""),
        "disable_web_page_preview": True,
    }
    if "parse_mode" in payload:
        data["parse_mode"] = payload["parse_mode"]

    try:
        response = requests.post(_API_URL, data=data, timeout=10)
        if response.status_code >= 400 and logger:
            logger.warning("Telegram send failed (%s): %s", response.status_code, response.text)
        return response.ok
    except Exception as exc:  # noqa: BLE001
        if logger:
            logger.error("Telegram request failed: %s", exc)
        return False


def send_trade_open_alert(
    *,
    symbol: str,
    price: float,
    qty: float,
    notional: float,
    stop: float,
    risk: float,
    tp1: float,
    regime: str,
    funding_rate: float,
    funding_boost: float,
    utc_time: str | None = None,
    logger: logging.Logger | None = None,
) -> bool:
    base = _base_asset(symbol)
    reward = tp1 - price
    boost_flag = "BOOST" if funding_boost > 1.0 else ""
    funding_pct = funding_rate * 100
    message = (
        "🟢 TRADE OPEN\n"
        f"Symbol: {symbol}\n"
        f"Entry: ${price:,.4f}\n"
        f"Size: {qty:,.4f} {base} ({_format_usd(notional)})\n"
        f"Stop: ${stop:,.4f} (-{_format_usd(risk)})\n"
        f"TP1: ${tp1:,.4f} (+{_format_usd(reward)})\n"
        f"Regime: {regime} | Funding: {funding_pct:.4f}% {boost_flag}\n"
        f"Time: {utc_time or _now_utc()}"
    )
    return _send_message({"text": message}, logger=logger)


def send_tp1_hit_alert(
    *,
    symbol: str,
    tp1: float,
    pnl: float,
    remaining_qty: float,
    base_asset: str | None = None,
    logger: logging.Logger | None = None,
) -> bool:
    base = base_asset or _base_asset(symbol)
    message = (
        "🟡 TP1 HIT\n"
        f"Symbol: {symbol}\n"
        f"TP1 filled: ${tp1:,.4f} | +{_format_usd(pnl)}\n"
        f"Remaining: {remaining_qty:,.4f} {base} | Trailing stop activated"
    )
    return _send_message({"text": message}, logger=logger)


def send_trade_closed_alert(
    *,
    symbol: str,
    price: float,
    reason: str,
    pnl: float,
    pct: float,
    hold_hours: float,
    logger: logging.Logger | None = None,
) -> bool:
    sign = "+" if pnl >= 0 else "-"
    message = (
        "🔴 TRADE CLOSED\n"
        f"Symbol: {symbol}\n"
        f"Exit: ${price:,.4f} ({reason})\n"
        f"P&L: {sign}{_format_usd(abs(pnl))} ({pct:+.2f}%)\n"
        f"Hold time: {hold_hours:.2f}h"
    )
    return _send_message({"text": message}, logger=logger)


def send_regime_change_alert(regime: str, *, utc_time: str | None = None, logger: logging.Logger | None = None) -> bool:
    message = (
        "🔄 REGIME CHANGE\n"
        "BTC EMA20/EMA50 crossed\n"
        f"New regime: {regime}\n"
        f"Time: {utc_time or _now_utc()}"
    )
    return _send_message({"text": message}, logger=logger)


def send_error_alert(
    *,
    component: str,
    error: str,
    action: str,
    logger: logging.Logger | None = None,
) -> bool:
    message = (
        "⚠️ ERROR\n"
        f"Component: {component}\n"
        f"Error: {error}\n"
        f"Action: {action}"
    )
    return _send_message({"text": message}, logger=logger)


def send_startup_alert(mode_label: str, *, logger: logging.Logger | None = None) -> bool:
    message = (
        "🚀 STRATEGY STARTUP\n"
        f"Mode: {mode_label}\n"
        f"Time: {_now_utc()}"
    )
    return _send_message({"text": message}, logger=logger)


def send_websocket_fallback_alert(*, logger: logging.Logger | None = None) -> bool:
    message = (
        "📡 WEBSOCKET FALLBACK\n"
        "Streaming failed repeatedly. Switching to REST polling every 4h."
    )
    return _send_message({"text": message}, logger=logger)
