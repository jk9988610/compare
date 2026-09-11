"""Directory compare engine: gitignore-aware unified / side-by-side diff."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from difflib import SequenceMatcher, unified_diff
from pathlib import Path

import pathspec

BINARY_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".bmp",
    ".pdf",
    ".zip",
    ".gz",
    ".7z",
    ".rar",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".bin",
    ".o",
    ".a",
    ".lib",
    ".class",
    ".pyc",
    ".pyo",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".eot",
    ".mp3",
    ".mp4",
    ".wav",
    ".avi",
    ".mov",
    ".sqlite",
    ".db",
}

KIND_LABEL = {
    "deleted": "删除",
    "added": "新增",
    "modified": "修改",
    "binary": "修改(二进制)",
}


@dataclass
class FileChange:
    rel: str
    kind: str
    diff_lines: list[str]
    lines_a: list[str] = field(default_factory=list)
    lines_b: list[str] = field(default_factory=list)
    path_a: str | None = None
    path_b: str | None = None


@dataclass
class CompareResult:
    error: str | None = None
    changes: list[FileChange] = field(default_factory=list)
    only_a: int = 0
    only_b: int = 0
    common: int = 0


@dataclass
class AlignedRow:
    left: str | None
    right: str | None
    kind: str  # equal | delete | insert | replace
    left_spans: list[tuple[int, int]] = field(default_factory=list)
    right_spans: list[tuple[int, int]] = field(default_factory=list)


def load_gitignore(root: Path) -> pathspec.PathSpec:
    patterns: list[str] = [".git/"]
    gi = root / ".gitignore"
    if gi.is_file():
        patterns.extend(gi.read_text(encoding="utf-8", errors="replace").splitlines())
    return pathspec.PathSpec.from_lines("gitwildmatch", patterns)


def collect_files(root: Path, spec: pathspec.PathSpec) -> dict[str, Path]:
    result: dict[str, Path] = {}
    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        rel_dir = base.relative_to(root)

        kept: list[str] = []
        for name in dirnames:
            rel = (rel_dir / name).as_posix() + "/"
            if not spec.match_file(rel):
                kept.append(name)
        dirnames[:] = kept

        for name in filenames:
            rel = (rel_dir / name).as_posix()
            if rel_dir == Path("."):
                rel = name
            if spec.match_file(rel):
                continue
            result[rel.replace("\\", "/")] = base / name
    return result


def is_binary(path: Path) -> bool:
    if path.suffix.lower() in BINARY_EXTENSIONS:
        return True
    try:
        with path.open("rb") as f:
            chunk = f.read(8192)
        return b"\x00" in chunk
    except OSError:
        return True


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def read_lines(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    if text and not text.endswith("\n"):
        text += "\n"
    return text.splitlines(keepends=True)


def plain_lines(kept: list[str]) -> list[str]:
    return [line.rstrip("\r\n") for line in kept]


def normalize_line(line: str, ignore_whitespace: bool) -> str:
    if not ignore_whitespace:
        return line
    return " ".join(line.split())


def lines_equivalent(a: list[str], b: list[str], ignore_whitespace: bool) -> bool:
    if not ignore_whitespace:
        return a == b
    return [normalize_line(x, True) for x in a] == [normalize_line(x, True) for x in b]


def _strip_diff(lines_iter) -> list[str]:
    return [line.rstrip("\n") for line in lines_iter]


def _char_spans(left: str, right: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    left_spans: list[tuple[int, int]] = []
    right_spans: list[tuple[int, int]] = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, left, right).get_opcodes():
        if tag in ("replace", "delete") and i1 != i2:
            left_spans.append((i1, i2))
        if tag in ("replace", "insert") and j1 != j2:
            right_spans.append((j1, j2))
    return left_spans, right_spans


def align_lines(
    lines_a: list[str],
    lines_b: list[str],
    ignore_whitespace: bool = False,
) -> list[AlignedRow]:
    keys_a = [normalize_line(x, ignore_whitespace) for x in lines_a]
    keys_b = [normalize_line(x, ignore_whitespace) for x in lines_b]
    rows: list[AlignedRow] = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, keys_a, keys_b).get_opcodes():
        if tag == "equal":
            for ia, ib in zip(range(i1, i2), range(j1, j2)):
                rows.append(AlignedRow(lines_a[ia], lines_b[ib], "equal"))
        elif tag == "delete":
            for ia in range(i1, i2):
                rows.append(AlignedRow(lines_a[ia], None, "delete"))
        elif tag == "insert":
            for ib in range(j1, j2):
                rows.append(AlignedRow(None, lines_b[ib], "insert"))
        else:  # replace
            left_block = lines_a[i1:i2]
            right_block = lines_b[j1:j2]
            n = max(len(left_block), len(right_block))
            for k in range(n):
                left = left_block[k] if k < len(left_block) else None
                right = right_block[k] if k < len(right_block) else None
                if left is None:
                    rows.append(AlignedRow(None, right, "insert"))
                elif right is None:
                    rows.append(AlignedRow(left, None, "delete"))
                else:
                    ls, rs = _char_spans(left, right)
                    rows.append(AlignedRow(left, right, "replace", ls, rs))
    return rows


def change_row_indices(rows: list[AlignedRow]) -> list[int]:
    return [i for i, row in enumerate(rows) if row.kind != "equal"]


def compute_diff(
    dir_a: Path,
    dir_b: Path,
    ignore_whitespace: bool = False,
) -> CompareResult:
    if not dir_a.is_dir():
        return CompareResult(error=f"不是目录: {dir_a}")
    if not dir_b.is_dir():
        return CompareResult(error=f"不是目录: {dir_b}")

    files_a = collect_files(dir_a, load_gitignore(dir_a))
    files_b = collect_files(dir_b, load_gitignore(dir_b))

    only_a = sorted(set(files_a) - set(files_b))
    only_b = sorted(set(files_b) - set(files_a))
    common = sorted(set(files_a) & set(files_b))
    changes: list[FileChange] = []

    for rel in only_a:
        path = files_a[rel]
        if is_binary(path):
            changes.append(
                FileChange(rel, "deleted", ["(二进制文件)"], path_a=str(path))
            )
            continue
        kept = read_lines(path)
        changes.append(
            FileChange(
                rel,
                "deleted",
                _strip_diff(unified_diff(kept, [], fromfile=f"a/{rel}", tofile=f"b/{rel}")),
                lines_a=plain_lines(kept),
                lines_b=[],
                path_a=str(path),
            )
        )

    for rel in only_b:
        path = files_b[rel]
        if is_binary(path):
            changes.append(
                FileChange(rel, "added", ["(二进制文件)"], path_b=str(path))
            )
            continue
        kept = read_lines(path)
        changes.append(
            FileChange(
                rel,
                "added",
                _strip_diff(unified_diff([], kept, fromfile=f"a/{rel}", tofile=f"b/{rel}")),
                lines_a=[],
                lines_b=plain_lines(kept),
                path_b=str(path),
            )
        )

    for rel in common:
        pa, pb = files_a[rel], files_b[rel]
        if is_binary(pa) or is_binary(pb):
            ha, hb = file_hash(pa), file_hash(pb)
            if ha != hb:
                changes.append(
                    FileChange(
                        rel,
                        "binary",
                        [f"a: {ha[:12]}...", f"b: {hb[:12]}..."],
                        path_a=str(pa),
                        path_b=str(pb),
                    )
                )
            continue

        kept_a = read_lines(pa)
        kept_b = read_lines(pb)
        if lines_equivalent(kept_a, kept_b, ignore_whitespace):
            continue
        changes.append(
            FileChange(
                rel,
                "modified",
                _strip_diff(
                    unified_diff(kept_a, kept_b, fromfile=f"a/{rel}", tofile=f"b/{rel}", n=3)
                ),
                lines_a=plain_lines(kept_a),
                lines_b=plain_lines(kept_b),
                path_a=str(pa),
                path_b=str(pb),
            )
        )

    return CompareResult(
        changes=changes,
        only_a=len(only_a),
        only_b=len(only_b),
        common=len(common),
    )


def build_unified_report(result: CompareResult) -> str:
    parts: list[str] = []
    for change in result.changes:
        parts.append(f"=== {KIND_LABEL[change.kind]}: {change.rel} ===")
        parts.extend(change.diff_lines)
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"
