"""
state_manager.py — Strategy State Persistence

Atomic read/write of strategy_state.json using temp file + rename pattern
to prevent data corruption on crash/interrupt.
"""

import json
import os
import logging
import tempfile
from datetime import datetime, timezone
from typing import Optional

from config import STATE_FILE

logger = logging.getLogger(__name__)


def default_state() -> dict:
    """Return a fresh empty strategy state."""
    return {
        "version": "1.0",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "btc_regime": "BEAR",
        "regime_updated_at": "",
        "open_positions": {},
        "closed_trades": [],
        "paper_mode": True,
        "paper_equity": 10000.0,
        "paper_pnl": 0.0,
        "lot_size_cache": {},
    }


def load_state(path: Optional[str] = None) -> dict:
    """
    Load strategy state from JSON file.

    Args:
        path: path to state file (default: STATE_FILE from config)

    Returns:
        State dict. Returns fresh default state if file is missing or corrupted.
    """
    state_path = path or STATE_FILE
    os.makedirs(os.path.dirname(state_path), exist_ok=True)

    if not os.path.exists(state_path):
        logger.warning("State file not found at %s — initializing fresh state", state_path)
        fresh = default_state()
        save_state(fresh, path=state_path)
        return fresh

    try:
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)
        logger.debug("State loaded from %s", state_path)
        return state
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "State file corrupted (%s) — reinitializing with empty state. Error: %s",
            state_path, exc,
        )
        fresh = default_state()
        save_state(fresh, path=state_path)
        return fresh


def save_state(state: dict, path: Optional[str] = None) -> None:
    """
    Atomically save strategy state to JSON file.

    Uses temp file + os.replace() to prevent partial writes on crash.

    Args:
        state: state dict to persist
        path: path to state file (default: STATE_FILE from config)
    """
    state_path = path or STATE_FILE
    os.makedirs(os.path.dirname(state_path), exist_ok=True)

    state["updated_at"] = datetime.now(timezone.utc).isoformat()

    dir_name = os.path.dirname(os.path.abspath(state_path))

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=dir_name,
            prefix=".state_tmp_",
            suffix=".json",
            delete=False,
            encoding="utf-8",
        ) as tmp_f:
            json.dump(state, tmp_f, indent=2, default=str)
            tmp_path = tmp_f.name

        os.replace(tmp_path, state_path)
        logger.debug("State saved to %s", state_path)
    except OSError as exc:
        logger.error("Failed to save state to %s: %s", state_path, exc)
        raise
