"""Persist GUI preferences (last directories, ignore rules, etc.)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

APP_NAME = "目录对比"


def app_data_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    path = Path(base) / APP_NAME
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path


SETTINGS_PATH = app_data_dir() / "settings.json"
_LEGACY_SETTINGS = Path(__file__).resolve().parent / "settings.json"

DEFAULTS: dict[str, Any] = {
    "dir_a": "",
    "dir_b": "",
    "ignore_whitespace": False,
    "ignore_comments": False,
    "word_wrap": True,
    "show_encoding_only": False,
    "geometry": "1200x760",
    "ignore_patterns": [],
    "ignore_rules": [],
    "sidebar_visible": True,
}


def _migrate_legacy_once() -> None:
    if SETTINGS_PATH.is_file() or not _LEGACY_SETTINGS.is_file():
        return
    try:
        SETTINGS_PATH.write_text(
            _LEGACY_SETTINGS.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    except OSError:
        pass


def load_settings() -> dict[str, Any]:
    _migrate_legacy_once()
    data = dict(DEFAULTS)
    try:
        if SETTINGS_PATH.is_file():
            raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for key in DEFAULTS:
                    if key not in raw:
                        continue
                    if key == "ignore_patterns":
                        pats = raw[key]
                        if isinstance(pats, list):
                            data[key] = [str(p) for p in pats]
                        elif isinstance(pats, str):
                            data[key] = [
                                ln.strip()
                                for ln in pats.splitlines()
                                if ln.strip() and not ln.strip().startswith("#")
                            ]
                    elif key == "ignore_rules":
                        rules = raw[key]
                        if isinstance(rules, list):
                            cleaned: list[dict[str, str]] = []
                            for item in rules:
                                if not isinstance(item, dict):
                                    continue
                                cleaned.append(
                                    {
                                        "folder": str(item.get("folder", "")),
                                        "types": str(item.get("types", "")),
                                    }
                                )
                            data[key] = cleaned
                    else:
                        data[key] = raw[key]
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return data


def save_settings(updates: dict[str, Any]) -> None:
    data = load_settings()
    data.update(updates)
    if "ignore_patterns" in data and isinstance(data["ignore_patterns"], str):
        data["ignore_patterns"] = [
            ln.strip()
            for ln in data["ignore_patterns"].splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass
