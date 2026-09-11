"""Persist GUI preferences (last directories, etc.)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SETTINGS_PATH = Path(__file__).resolve().parent / "settings.json"

DEFAULTS: dict[str, Any] = {
    "dir_a": "",
    "dir_b": "",
    "auto_refresh": True,
    "ignore_whitespace": False,
    "geometry": "1200x760",
}


def load_settings() -> dict[str, Any]:
    data = dict(DEFAULTS)
    try:
        if SETTINGS_PATH.is_file():
            raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for key in DEFAULTS:
                    if key in raw:
                        data[key] = raw[key]
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return data


def save_settings(updates: dict[str, Any]) -> None:
    data = load_settings()
    data.update(updates)
    try:
        SETTINGS_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass
