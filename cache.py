"""Persist last CompareResult so GUI can reopen without rescanning."""

from __future__ import annotations

import gzip
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from engine import CompareResult, EncodingEntry, FileChange
from settings import app_data_dir

CACHE_PATH = app_data_dir() / "last_compare.json.gz"
CACHE_VERSION = 1


def _norm_dir(path: str | Path) -> str:
    try:
        return str(Path(path).resolve())
    except OSError:
        return str(path).strip()


def cache_fingerprint(
    dir_a: str | Path,
    dir_b: str | Path,
    *,
    ignore_whitespace: bool,
    ignore_comments: bool,
    ignore_patterns: list[str] | None = None,
) -> dict[str, Any]:
    pats = [str(p) for p in (ignore_patterns or [])]
    return {
        "dir_a": _norm_dir(dir_a),
        "dir_b": _norm_dir(dir_b),
        "ignore_whitespace": bool(ignore_whitespace),
        "ignore_comments": bool(ignore_comments),
        "ignore_patterns": pats,
    }


def _result_to_dict(result: CompareResult) -> dict[str, Any]:
    return {
        "error": result.error,
        "only_a": result.only_a,
        "only_b": result.only_b,
        "common": result.common,
        "changes": [asdict(c) for c in result.changes],
        "encoding_report": [asdict(e) for e in result.encoding_report],
    }


def _result_from_dict(data: dict[str, Any]) -> CompareResult:
    changes: list[FileChange] = []
    for raw in data.get("changes") or []:
        if not isinstance(raw, dict):
            continue
        changes.append(
            FileChange(
                rel=str(raw.get("rel", "")),
                kind=str(raw.get("kind", "modified")),
                diff_lines=[str(x) for x in (raw.get("diff_lines") or [])],
                lines_a=[str(x) for x in (raw.get("lines_a") or [])],
                lines_b=[str(x) for x in (raw.get("lines_b") or [])],
                path_a=raw.get("path_a"),
                path_b=raw.get("path_b"),
                encoding_a=raw.get("encoding_a"),
                encoding_b=raw.get("encoding_b"),
                encoding_only=bool(raw.get("encoding_only", False)),
                encoding_note=str(raw.get("encoding_note") or ""),
                encoding_reason=str(raw.get("encoding_reason") or ""),
            )
        )
    report: list[EncodingEntry] = []
    for raw in data.get("encoding_report") or []:
        if not isinstance(raw, dict):
            continue
        report.append(
            EncodingEntry(
                rel=str(raw.get("rel", "")),
                side=str(raw.get("side", "both")),
                encoding=str(raw.get("encoding", "")),
                ambiguous=bool(raw.get("ambiguous", False)),
                warning=str(raw.get("warning") or ""),
                encoding_a=raw.get("encoding_a"),
                encoding_b=raw.get("encoding_b"),
                encoding_only=bool(raw.get("encoding_only", False)),
            )
        )
    return CompareResult(
        error=data.get("error"),
        changes=changes,
        only_a=int(data.get("only_a") or 0),
        only_b=int(data.get("only_b") or 0),
        common=int(data.get("common") or 0),
        encoding_report=report,
    )


def save_compare_cache(
    fingerprint: dict[str, Any],
    result: CompareResult,
    *,
    selected_rel: str | None = None,
) -> None:
    if result.error:
        return
    payload = {
        "version": CACHE_VERSION,
        "fingerprint": fingerprint,
        "selected_rel": selected_rel or "",
        "result": _result_to_dict(result),
    }
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        with gzip.open(CACHE_PATH, "wb") as fh:
            fh.write(raw)
    except OSError:
        pass


def load_compare_cache(fingerprint: dict[str, Any]) -> tuple[CompareResult, str] | None:
    if not CACHE_PATH.is_file():
        return None
    try:
        with gzip.open(CACHE_PATH, "rb") as fh:
            payload = json.loads(fh.read().decode("utf-8"))
    except (OSError, EOFError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or int(payload.get("version") or 0) != CACHE_VERSION:
        return None
    saved_fp = payload.get("fingerprint")
    if not isinstance(saved_fp, dict):
        return None
    for key in ("dir_a", "dir_b", "ignore_whitespace", "ignore_comments", "ignore_patterns"):
        if saved_fp.get(key) != fingerprint.get(key):
            return None
    result_raw = payload.get("result")
    if not isinstance(result_raw, dict):
        return None
    result = _result_from_dict(result_raw)
    if result.error:
        return None
    selected = str(payload.get("selected_rel") or "")
    return result, selected
