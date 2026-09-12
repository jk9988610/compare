"""Bridge to encoding_unify (sibling tool under E:\\tools). Diff stays read-only for compare itself."""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_TOOLS = Path(r"E:\tools")
_UNIFY = _TOOLS / "encoding_unify"

# Upstream recommendation for a clean Diff: UTF-8 no BOM + LF on both sides.
TARGET_ENCODING = "utf-8"
TARGET_NEWLINE = "\n"


def unify_available() -> bool:
    return (_UNIFY / "core.py").is_file()


def _ensure_import() -> Any:
    if not unify_available():
        raise FileNotFoundError(f"未找到 encoding_unify：{_UNIFY}")
    root = str(_TOOLS)
    if root not in sys.path:
        sys.path.insert(0, root)
    from encoding_unify import core as unify_core  # type: ignore

    return unify_core


def _file_needs_eol_or_bom(root: Path, rel: str) -> bool:
    path = root / rel
    try:
        data = path.read_bytes()
    except OSError:
        return False
    if data.startswith(b"\xef\xbb\xbf"):
        return True
    return b"\r" in data


def _from_encoding_for_convert(enc: str) -> str | None:
    if enc in {"binary", "unknown"}:
        return None
    if enc == "mixed-suspect":
        return "utf-8"
    return enc


@dataclass
class PrepSummary:
    left_root: str
    right_root: str
    left_total: int
    right_total: int
    left_to_convert: int
    right_to_convert: int
    left_review: int
    right_review: int
    left_eol_or_bom: int = 0
    right_eol_or_bom: int = 0
    left_by_encoding: dict[str, int] = field(default_factory=dict)
    right_by_encoding: dict[str, int] = field(default_factory=dict)
    left_errors: list[str] = field(default_factory=list)
    right_errors: list[str] = field(default_factory=list)
    applied: bool = False
    normalized_lf: bool = False

    @property
    def pending(self) -> int:
        return (
            self.left_to_convert
            + self.right_to_convert
            + self.left_review
            + self.right_review
            + self.left_eol_or_bom
            + self.right_eol_or_bom
        )

    @property
    def ok_for_diff(self) -> bool:
        return self.pending == 0 and not self.left_errors and not self.right_errors

    def text(self) -> str:
        mode = "已统一为 UTF-8 无 BOM + LF" if self.applied else "仅检查"
        lines = [
            f"{mode}",
            "",
            f"旧侧: {self.left_root}",
            f"  文件 {self.left_total} · 待转码 {self.left_to_convert} · "
            f"待复核 {self.left_review} · 待统一换行/BOM {self.left_eol_or_bom}",
            f"  分布 {self.left_by_encoding or '{}'}",
            "",
            f"新侧: {self.right_root}",
            f"  文件 {self.right_total} · 待转码 {self.right_to_convert} · "
            f"待复核 {self.right_review} · 待统一换行/BOM {self.right_eol_or_bom}",
            f"  分布 {self.right_by_encoding or '{}'}",
        ]
        if self.left_errors or self.right_errors:
            lines.append("")
            lines.append("错误（节选）:")
            for e in (self.left_errors + self.right_errors)[:8]:
                lines.append(f"  - {e}")
        lines.append("")
        if self.ok_for_diff:
            lines.append("结论: 可以开始 Diff（编码与换行已对齐或无需处理）。")
        else:
            lines.append(
                "结论: 仍有待处理项。建议「统一 UTF-8+LF 后运行」，"
                "使两侧均为 UTF-8 无 BOM + LF，Diff 更干净。"
            )
        return "\n".join(lines)


def _count_eol_bom(root: Path, report: Any) -> int:
    n = 0
    root_p = Path(report.root)
    for info in report.files:
        if info.encoding in {"binary", "unknown"}:
            continue
        # Already queued for charset convert — counted elsewhere.
        if info.action == "convert":
            continue
        if _file_needs_eol_or_bom(root_p, info.path):
            n += 1
    return n


def _apply_utf8_lf(core: Any, report: Any) -> None:
    """Write every convertible text file as UTF-8 no BOM + LF (with backup)."""
    root_p = Path(report.root)
    backup_dir = root_p / "_encoding_backup"
    for info in report.files:
        from_enc = _from_encoding_for_convert(info.encoding)
        if from_enc is None:
            continue
        full = root_p / info.path
        if not full.is_file():
            continue
        needs_charset = info.action == "convert" or from_enc in {
            "gbk",
            "gb18030",
            "utf-8-sig",
        }
        needs_eol = _file_needs_eol_or_bom(root_p, info.path)
        if not needs_charset and not needs_eol:
            continue
        try:
            core.convert_file(
                full,
                from_encoding=from_enc,
                to_encoding=TARGET_ENCODING,
                newline=TARGET_NEWLINE,
                backup_dir=backup_dir,
            )
            info.encoding = TARGET_ENCODING
            info.has_bom = False
            info.action = "converted"
            info.note = (info.note + " | utf-8+lf").strip(" |")
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"{info.path}: normalize failed: {exc}")
            info.action = "review"


def prepare_for_diff(
    left: Path,
    right: Path,
    *,
    apply: bool = False,
    newline: str = TARGET_NEWLINE,
) -> PrepSummary:
    """
    apply=False: scan only (safe), also count EOL/BOM residuals on utf-8 files.
    apply=True: rewrite both trees to UTF-8 no BOM + LF (default), with _encoding_backup.
    """
    core = _ensure_import()
    # Left/right trees are independent — scan in parallel.
    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_l = pool.submit(core.scan_tree, left, TARGET_ENCODING)
        fut_r = pool.submit(core.scan_tree, right, TARGET_ENCODING)
        left_r = fut_l.result()
        right_r = fut_r.result()

    left_eol = _count_eol_bom(Path(left_r.root), left_r)
    right_eol = _count_eol_bom(Path(right_r.root), right_r)

    if apply:
        _apply_utf8_lf(core, left_r)
        _apply_utf8_lf(core, right_r)
        # Re-scan so summary reflects post-normalize state.
        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_l = pool.submit(core.scan_tree, left, TARGET_ENCODING)
            fut_r = pool.submit(core.scan_tree, right, TARGET_ENCODING)
            left_r = fut_l.result()
            right_r = fut_r.result()
        left_eol = _count_eol_bom(Path(left_r.root), left_r)
        right_eol = _count_eol_bom(Path(right_r.root), right_r)

    ls, rs = left_r.summary(), right_r.summary()
    return PrepSummary(
        left_root=str(left_r.root),
        right_root=str(right_r.root),
        left_total=int(ls.get("total", 0)),
        right_total=int(rs.get("total", 0)),
        left_to_convert=int(ls.get("to_convert", 0)),
        right_to_convert=int(rs.get("to_convert", 0)),
        left_review=int(ls.get("need_review", 0)),
        right_review=int(rs.get("need_review", 0)),
        left_eol_or_bom=left_eol,
        right_eol_or_bom=right_eol,
        left_by_encoding=dict(ls.get("by_encoding") or {}),
        right_by_encoding=dict(rs.get("by_encoding") or {}),
        left_errors=list(left_r.errors),
        right_errors=list(right_r.errors),
        applied=apply,
        normalized_lf=apply and newline == "\n",
    )
