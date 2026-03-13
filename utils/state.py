"""State persistence helpers."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def save_state(state: dict[str, Any], path: str | Path = "state/strategy_state.json") -> None:
    """Persist strategy state atomically.

    Args:
        state: Arbitrary state dictionary.
        path: Destination JSON path (defaults to ``state/strategy_state.json``).
    """

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    dir_name = os.path.dirname(os.path.abspath(path))
    with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, suffix=".tmp") as tmp_file:
        json.dump(state, tmp_file, indent=2, default=str)
        tmp_path = tmp_file.name
    os.replace(tmp_path, path)
