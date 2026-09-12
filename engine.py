"""Directory compare engine: gitignore-aware unified / side-by-side diff."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from difflib import SequenceMatcher, unified_diff
from pathlib import Path

import pathspec

from comments import (
    comment_style_for,
    looks_like_comment_line,
    map_spans_to_original,
    strip_comments_from_lines,
    strip_line_with_index_map,
)
from encodingutil import DecodeResult, normalize_newlines, read_text_auto

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
    "encoding": "仅编码",
}


@dataclass
class EncodingEntry:
    rel: str
    side: str  # a | b | both
    encoding: str
    ambiguous: bool = False
    warning: str = ""
    encoding_a: str | None = None
    encoding_b: str | None = None
    encoding_only: bool = False


@dataclass
class FileChange:
    rel: str
    kind: str
    diff_lines: list[str]
    lines_a: list[str] = field(default_factory=list)
    lines_b: list[str] = field(default_factory=list)
    path_a: str | None = None
    path_b: str | None = None
    encoding_a: str | None = None
    encoding_b: str | None = None
    encoding_only: bool = False
    encoding_note: str = ""
    # eol | bom | eol+bom | bytes | charset — why bytes differ when Unicode matches
    encoding_reason: str = ""


@dataclass
class CompareResult:
    error: str | None = None
    changes: list[FileChange] = field(default_factory=list)
    only_a: int = 0
    only_b: int = 0
    common: int = 0
    encoding_report: list[EncodingEntry] = field(default_factory=list)


@dataclass
class AlignedRow:
    left: str | None
    right: str | None
    kind: str  # equal | delete | insert | replace
    left_spans: list[tuple[int, int]] = field(default_factory=list)
    right_spans: list[tuple[int, int]] = field(default_factory=list)
    # 1-based line numbers in the original files (None = blank / no source line).
    left_no: int | None = None
    right_no: int | None = None


def load_gitignore(
    root: Path,
    extra_patterns: list[str] | None = None,
) -> pathspec.PathSpec:
    patterns: list[str] = [".git/", "_encoding_backup/"]
    gi = root / ".gitignore"
    if gi.is_file():
        patterns.extend(gi.read_text(encoding="utf-8", errors="replace").splitlines())
    if extra_patterns:
        patterns.extend(extra_patterns)
    return pathspec.PathSpec.from_lines("gitwildmatch", patterns)


def _io_workers() -> int:
    """Thread count for parallel file reads (I/O-bound)."""
    n = os.cpu_count() or 4
    return min(32, max(4, n * 2))


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


def read_lines(path: Path) -> tuple[list[str], DecodeResult]:
    """Read file as Unicode lines with \\n newlines; never use replace mojibake."""
    decoded = read_text_auto(path)
    text = normalize_newlines(decoded.text)
    if text and not text.endswith("\n"):
        text += "\n"
    return text.splitlines(keepends=True), decoded


def plain_lines(kept: list[str]) -> list[str]:
    return [line.rstrip("\n") for line in kept]


def classify_encoding_only_reason(path_a: Path, path_b: Path) -> str:
    """
    When Unicode text matches but file bytes differ, classify the residual cause.
    Returns e.g. 'eol', 'bom', 'eol+bom', 'bytes', 'charset'.
    """
    try:
        ba = path_a.read_bytes()
        bb = path_b.read_bytes()
    except OSError:
        return "bytes"
    if ba == bb:
        return ""

    bom = b"\xef\xbb\xbf"
    reasons: list[str] = []
    a_bom, b_bom = ba.startswith(bom), bb.startswith(bom)
    if a_bom != b_bom:
        reasons.append("bom")

    na = ba[len(bom) :] if a_bom else ba
    nb = bb[len(bom) :] if b_bom else bb
    na_lf = na.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    nb_lf = nb.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    if na != nb and na_lf == nb_lf:
        reasons.append("eol")
    elif na_lf != nb_lf:
        # Same Unicode after our decode path, but raw bytes still differ beyond EOL/BOM.
        reasons.append("bytes")

    if not reasons:
        reasons.append("bytes")
    return "+".join(reasons)


def _sample_from_lines(lines: list[str], limit: int = 40) -> str:
    plain = [line.rstrip("\r\n") for line in lines[:limit]]
    return "\n".join(plain)


def normalize_ws(line: str, ignore_whitespace: bool) -> str:
    if not ignore_whitespace:
        return line
    # Collapse whitespace runs and spaces around , ;
    # so "6600," and "6600 ," compare equal when ignoring whitespace.
    s = " ".join(line.split())
    s = re.sub(r"\s*,\s*", ",", s)
    s = re.sub(r"\s*;\s*", ";", s)
    return s


def _normalize_code_key(code: str, ignore_whitespace: bool) -> str:
    """Trim and optionally compress whitespace in code after comment strip."""
    return normalize_ws(code.strip(), ignore_whitespace)


def _is_blank_line(line: str) -> bool:
    return line.rstrip("\r\n").strip() == ""


def _drop_blank_keys(keys: list[str]) -> list[str]:
    """Remove blank-line keys when ignoring whitespace (empty-string keys)."""
    return [k for k in keys if k.strip() != ""]


def normalize_lines(
    lines: list[str],
    *,
    rel: str = "",
    ignore_whitespace: bool = False,
    ignore_comments: bool = False,
    comment_key: str = "text",
) -> list[str]:
    """
    Build comparison keys for lines.
    comment_key:
      - \"text\": comment-only lines keyed by their text (for side-by-side align)
      - \"slot\": all comment-only lines share one marker (for file equivalence:
        comment *edits* match; comment add/remove change structure)
    When ignore_whitespace is set, callers should also drop blank lines from
    matching (see lines_equivalent / align_lines).
    """
    plain = [line.rstrip("\r\n") for line in lines]
    if not ignore_comments:
        return [normalize_ws(line, ignore_whitespace) for line in plain]

    sample = _sample_from_lines(plain)
    style = comment_style_for(rel, sample)
    stripped = strip_comments_from_lines(plain, style)
    # Ignoring comments: also normalize incidental whitespace left by inserting
    # /* */ or // on the same line (e.g. "6600," vs "/*x*/ 6600 , //y").
    soften_ws = ignore_whitespace or ignore_comments
    keys: list[str] = []
    for orig, core in zip(plain, stripped):
        code = _normalize_code_key(core, soften_ws)
        if code:
            keys.append(code)
            continue
        if not orig.strip():
            keys.append("")
            continue
        if comment_key == "slot":
            keys.append("\x00C")
        else:
            text = orig.strip()
            keys.append(_normalize_code_key(text, ignore_whitespace))
    return keys


def lines_equivalent(
    a: list[str],
    b: list[str],
    *,
    rel: str = "",
    ignore_whitespace: bool = False,
    ignore_comments: bool = False,
) -> bool:
    if not ignore_whitespace and not ignore_comments:
        return a == b
    # slot keys: comment-only edits compare equal; add/remove comments do not.
    keys_a = normalize_lines(
        a,
        rel=rel,
        ignore_whitespace=ignore_whitespace,
        ignore_comments=ignore_comments,
        comment_key="slot" if ignore_comments else "text",
    )
    keys_b = normalize_lines(
        b,
        rel=rel,
        ignore_whitespace=ignore_whitespace,
        ignore_comments=ignore_comments,
        comment_key="slot" if ignore_comments else "text",
    )
    if ignore_whitespace:
        keys_a = _drop_blank_keys(keys_a)
        keys_b = _drop_blank_keys(keys_b)
    return keys_a == keys_b


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


def _char_spans_code_only(
    left: str, right: str, style: str
) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Diff after stripping comments; project highlights onto code regions only."""
    left_code, left_map = strip_line_with_index_map(left, style)
    right_code, right_map = strip_line_with_index_map(right, style)
    left_spans, right_spans = _char_spans(left_code, right_code)
    return (
        map_spans_to_original(left_spans, left_map),
        map_spans_to_original(right_spans, right_map),
    )


def _line_is_comment_only(line: str | None, style: str) -> bool:
    """True for comment-only / comment-continuation lines (ignore blank)."""
    if line is None or style == "none":
        return False
    raw = line.rstrip("\r\n")
    if not raw.strip():
        return False
    if looks_like_comment_line(raw, style):
        return True
    code, _ = strip_line_with_index_map(raw, style)
    return code.strip() == ""


def _codes_equal_ignoring_comments(
    left: str,
    right: str,
    *,
    style: str,
    ignore_whitespace: bool,
) -> bool:
    """True when code is the same after stripping comments (incl. inline /* */)."""
    if style == "none":
        return False
    left_code, _ = strip_line_with_index_map(left.rstrip("\r\n"), style)
    right_code, _ = strip_line_with_index_map(right.rstrip("\r\n"), style)
    # Always soften whitespace here: inserting /* */ / // often adds spaces
    # (e.g. "6600," vs "/*x*/ 6600 , //y") that are not real code changes.
    left_code = _normalize_code_key(left_code, ignore_whitespace=True)
    right_code = _normalize_code_key(right_code, ignore_whitespace=True)
    return left_code == right_code


def _collapse_ignored_comment_rows(
    rows: list[AlignedRow],
    *,
    style: str,
    ignore_whitespace: bool,
) -> list[AlignedRow]:
    """
    Drop markings for pure comment edits:
    - comment-only replace / del+ins pairs
    - same-line code with only trailing-comment text changed
    """
    out: list[AlignedRow] = []
    i = 0
    n = len(rows)
    while i < n:
        cur = rows[i]
        nxt = rows[i + 1] if i + 1 < n else None

        if (
            nxt is not None
            and cur.kind == "delete"
            and nxt.kind == "insert"
            and _line_is_comment_only(cur.left, style)
            and _line_is_comment_only(nxt.right, style)
        ):
            out.append(
                AlignedRow(
                    cur.left,
                    nxt.right,
                    "equal",
                    [],
                    [],
                    cur.left_no,
                    nxt.right_no,
                )
            )
            i += 2
            continue

        if (
            cur.kind == "replace"
            and cur.left is not None
            and cur.right is not None
            and (
                (
                    _line_is_comment_only(cur.left, style)
                    and _line_is_comment_only(cur.right, style)
                )
                or _codes_equal_ignoring_comments(
                    cur.left,
                    cur.right,
                    style=style,
                    ignore_whitespace=ignore_whitespace,
                )
            )
        ):
            out.append(
                AlignedRow(
                    cur.left,
                    cur.right,
                    "equal",
                    [],
                    [],
                    cur.left_no,
                    cur.right_no,
                )
            )
            i += 1
            continue

        out.append(cur)
        i += 1
    return out


def align_lines(
    lines_a: list[str],
    lines_b: list[str],
    ignore_whitespace: bool = False,
    ignore_comments: bool = False,
    rel: str = "",
) -> list[AlignedRow]:
    # Ignore blank lines as part of "ignore whitespace": align on non-blank
    # lines only, then map line numbers back to the original files.
    if ignore_whitespace:
        map_a = [i for i, ln in enumerate(lines_a) if not _is_blank_line(ln)]
        map_b = [i for i, ln in enumerate(lines_b) if not _is_blank_line(ln)]
        sub_a = [lines_a[i] for i in map_a]
        sub_b = [lines_b[i] for i in map_b]
        rows = _align_lines_body(
            sub_a,
            sub_b,
            ignore_whitespace=True,
            ignore_comments=ignore_comments,
            rel=rel,
        )
        remapped: list[AlignedRow] = []
        for r in rows:
            left_no = map_a[r.left_no - 1] + 1 if r.left_no is not None else None
            right_no = map_b[r.right_no - 1] + 1 if r.right_no is not None else None
            remapped.append(
                AlignedRow(
                    r.left,
                    r.right,
                    r.kind,
                    r.left_spans,
                    r.right_spans,
                    left_no,
                    right_no,
                )
            )
        return remapped

    return _align_lines_body(
        lines_a,
        lines_b,
        ignore_whitespace=False,
        ignore_comments=ignore_comments,
        rel=rel,
    )


def _align_lines_body(
    lines_a: list[str],
    lines_b: list[str],
    ignore_whitespace: bool = False,
    ignore_comments: bool = False,
    rel: str = "",
) -> list[AlignedRow]:
    keys_a = normalize_lines(
        lines_a,
        rel=rel,
        ignore_whitespace=ignore_whitespace,
        ignore_comments=ignore_comments,
        # Text keys for align: identical markers (e.g. " *", "*/") pair correctly.
        # Slot keys made every comment match every other comment and orphan closers.
        comment_key="text",
    )
    keys_b = normalize_lines(
        lines_b,
        rel=rel,
        ignore_whitespace=ignore_whitespace,
        ignore_comments=ignore_comments,
        comment_key="text",
    )
    sample = _sample_from_lines(lines_a) or _sample_from_lines(lines_b)
    style = comment_style_for(rel, sample)

    # Stateful strip — required for block-comment continuations like " * @brief".
    stripped_a = strip_comments_from_lines(
        [ln.rstrip("\r\n") for ln in lines_a], style
    )
    stripped_b = strip_comments_from_lines(
        [ln.rstrip("\r\n") for ln in lines_b], style
    )
    comment_only_a = [
        bool(orig.rstrip("\r\n").strip())
        and (not code.strip() or looks_like_comment_line(orig, style))
        for orig, code in zip(lines_a, stripped_a)
    ]
    comment_only_b = [
        bool(orig.rstrip("\r\n").strip())
        and (not code.strip() or looks_like_comment_line(orig, style))
        for orig, code in zip(lines_b, stripped_b)
    ]

    def row(
        left: str | None,
        right: str | None,
        kind: str,
        *,
        ia: int | None = None,
        ib: int | None = None,
        left_spans: list[tuple[int, int]] | None = None,
        right_spans: list[tuple[int, int]] | None = None,
    ) -> AlignedRow:
        return AlignedRow(
            left,
            right,
            kind,
            left_spans or [],
            right_spans or [],
            left_no=(ia + 1) if left is not None and ia is not None else None,
            right_no=(ib + 1) if right is not None and ib is not None else None,
        )

    def both_comment_only(ia: int | None, ib: int | None) -> bool:
        if ia is None or ib is None:
            return False
        return comment_only_a[ia] and comment_only_b[ib]

    def only_comment_text_changed(left: str, right: str) -> bool:
        return _codes_equal_ignoring_comments(
            left,
            right,
            style=style,
            ignore_whitespace=ignore_whitespace,
        )

    rows: list[AlignedRow] = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, keys_a, keys_b).get_opcodes():
        if tag == "equal":
            for ia, ib in zip(range(i1, i2), range(j1, j2)):
                left, right = lines_a[ia], lines_b[ib]
                if ignore_comments and left != right and (
                    both_comment_only(ia, ib) or only_comment_text_changed(left, right)
                ):
                    rows.append(row(left, right, "equal", ia=ia, ib=ib))
                elif ignore_comments and left != right and style != "none":
                    ls, rs = _char_spans_code_only(left, right, style)
                    if not ls and not rs:
                        rows.append(row(left, right, "equal", ia=ia, ib=ib))
                    else:
                        rows.append(
                            row(
                                left,
                                right,
                                "replace",
                                ia=ia,
                                ib=ib,
                                left_spans=ls,
                                right_spans=rs,
                            )
                        )
                else:
                    rows.append(row(left, right, "equal", ia=ia, ib=ib))
        elif tag == "delete":
            for ia in range(i1, i2):
                rows.append(row(lines_a[ia], None, "delete", ia=ia))
        elif tag == "insert":
            for ib in range(j1, j2):
                rows.append(row(None, lines_b[ib], "insert", ib=ib))
        else:  # replace
            left_block = lines_a[i1:i2]
            right_block = lines_b[j1:j2]
            n = max(len(left_block), len(right_block))
            for k in range(n):
                ia = i1 + k if k < len(left_block) else None
                ib = j1 + k if k < len(right_block) else None
                left = left_block[k] if k < len(left_block) else None
                right = right_block[k] if k < len(right_block) else None
                if left is None:
                    rows.append(row(None, right, "insert", ib=ib))
                elif right is None:
                    rows.append(row(left, None, "delete", ia=ia))
                elif not left.strip() and right.strip():
                    rows.append(row(None, right, "insert", ib=ib))
                elif left.strip() and not right.strip():
                    rows.append(row(left, None, "delete", ia=ia))
                else:
                    if ignore_comments and (
                        both_comment_only(ia, ib)
                        or only_comment_text_changed(left, right)
                    ):
                        rows.append(row(left, right, "equal", ia=ia, ib=ib))
                        continue
                    if ignore_comments and style != "none":
                        ls, rs = _char_spans_code_only(left, right, style)
                        if not ls and not rs:
                            rows.append(row(left, right, "equal", ia=ia, ib=ib))
                            continue
                    else:
                        ls, rs = _char_spans(left, right)
                    rows.append(
                        row(
                            left,
                            right,
                            "replace",
                            ia=ia,
                            ib=ib,
                            left_spans=ls,
                            right_spans=rs,
                        )
                    )

    if ignore_comments and style != "none":
        rows = _collapse_ignored_comment_rows(
            rows, style=style, ignore_whitespace=ignore_whitespace
        )
    return rows


def change_row_indices(rows: list[AlignedRow]) -> list[int]:
    return [i for i, row in enumerate(rows) if row.kind != "equal"]


def compute_diff(
    dir_a: Path,
    dir_b: Path,
    ignore_whitespace: bool = False,
    ignore_comments: bool = False,
    extra_ignore: list[str] | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> CompareResult:
    if not dir_a.is_dir():
        return CompareResult(error=f"不是目录: {dir_a}")
    if not dir_b.is_dir():
        return CompareResult(error=f"不是目录: {dir_b}")

    files_a = collect_files(dir_a, load_gitignore(dir_a, extra_ignore))
    files_b = collect_files(dir_b, load_gitignore(dir_b, extra_ignore))

    only_a = sorted(set(files_a) - set(files_b))
    only_b = sorted(set(files_b) - set(files_a))
    common = sorted(set(files_a) & set(files_b))
    changes: list[FileChange] = []
    encoding_report: list[EncodingEntry] = []
    total = len(only_a) + len(only_b) + len(common)
    done = 0

    def _tick(rel: str) -> None:
        nonlocal done
        done += 1
        if progress is not None:
            progress(done, total, rel)

    def _entry_one(rel: str, side: str, dec: DecodeResult) -> EncodingEntry:
        return EncodingEntry(
            rel=rel,
            side=side,
            encoding=dec.encoding,
            ambiguous=dec.ambiguous,
            warning=dec.warning,
            encoding_a=dec.encoding if side == "a" else None,
            encoding_b=dec.encoding if side == "b" else None,
        )

    def _entry_both(
        rel: str,
        dec: DecodeResult,
        other: DecodeResult,
        *,
        encoding_only: bool = False,
    ) -> EncodingEntry:
        warn_parts = [w for w in (dec.warning, other.warning) if w]
        if dec.encoding != other.encoding:
            warn_parts.append(
                f"左右编码不一致（{dec.encoding} vs {other.encoding}）"
            )
        return EncodingEntry(
            rel=rel,
            side="both",
            encoding=f"{dec.encoding} / {other.encoding}",
            ambiguous=dec.ambiguous or other.ambiguous,
            warning="; ".join(warn_parts),
            encoding_a=dec.encoding,
            encoding_b=other.encoding,
            encoding_only=encoding_only,
        )

    def _work_only_a(rel: str) -> tuple[FileChange, EncodingEntry | None]:
        path = files_a[rel]
        if is_binary(path):
            return (
                FileChange(rel, "deleted", ["(二进制文件)"], path_a=str(path)),
                None,
            )
        kept, dec = read_lines(path)
        plain = plain_lines(kept)
        note = f"# left: {dec.encoding} -> unicode"
        if dec.warning:
            note += f"\n# warning: {dec.warning}"
        return (
            FileChange(
                rel,
                "deleted",
                make_unified_diff_lines(
                    plain,
                    [],
                    rel=rel,
                    ignore_whitespace=ignore_whitespace,
                    ignore_comments=ignore_comments,
                ),
                lines_a=plain,
                lines_b=[],
                path_a=str(path),
                encoding_a=dec.encoding,
                encoding_note=note,
            ),
            _entry_one(rel, "a", dec),
        )

    def _work_only_b(rel: str) -> tuple[FileChange, EncodingEntry | None]:
        path = files_b[rel]
        if is_binary(path):
            return (
                FileChange(rel, "added", ["(二进制文件)"], path_b=str(path)),
                None,
            )
        kept, dec = read_lines(path)
        plain = plain_lines(kept)
        note = f"# right: {dec.encoding} -> unicode"
        if dec.warning:
            note += f"\n# warning: {dec.warning}"
        return (
            FileChange(
                rel,
                "added",
                make_unified_diff_lines(
                    [],
                    plain,
                    rel=rel,
                    ignore_whitespace=ignore_whitespace,
                    ignore_comments=ignore_comments,
                ),
                lines_a=[],
                lines_b=plain,
                path_b=str(path),
                encoding_b=dec.encoding,
                encoding_note=note,
            ),
            _entry_one(rel, "b", dec),
        )

    def _work_common(
        rel: str,
    ) -> tuple[FileChange | None, EncodingEntry | None]:
        pa, pb = files_a[rel], files_b[rel]
        if is_binary(pa) or is_binary(pb):
            ha, hb = file_hash(pa), file_hash(pb)
            if ha != hb:
                return (
                    FileChange(
                        rel,
                        "binary",
                        [f"a: {ha[:12]}...", f"b: {hb[:12]}..."],
                        path_a=str(pa),
                        path_b=str(pb),
                    ),
                    None,
                )
            return None, None

        kept_a, dec_a = read_lines(pa)
        kept_b, dec_b = read_lines(pb)
        plain_a = plain_lines(kept_a)
        plain_b = plain_lines(kept_b)

        enc_mismatch = dec_a.encoding != dec_b.encoding
        notes = [
            f"# left: {dec_a.encoding} -> unicode",
            f"# right: {dec_b.encoding} -> unicode",
        ]
        if enc_mismatch:
            notes.append(
                f"# warning: 左右编码不一致（{dec_a.encoding} vs {dec_b.encoding}）"
            )
        if dec_a.warning:
            notes.append(f"# warning-left: {dec_a.warning}")
        if dec_b.warning:
            notes.append(f"# warning-right: {dec_b.warning}")

        if plain_a == plain_b:
            only_enc = enc_mismatch or file_hash(pa) != file_hash(pb)
            entry = _entry_both(rel, dec_a, dec_b, encoding_only=only_enc)
            if only_enc:
                reason = classify_encoding_only_reason(pa, pb)
                if enc_mismatch and "charset" not in reason:
                    reason = f"charset+{reason}" if reason else "charset"
                reason_label = {
                    "eol": "换行符不同（CRLF vs LF）",
                    "bom": "BOM 有无不同",
                    "eol+bom": "换行符与 BOM 不同",
                    "bom+eol": "换行符与 BOM 不同",
                    "bytes": "其它字节级差异",
                    "charset": "探测编码标签不同",
                }.get(reason, reason)
                notes.append(
                    "# encoding-only: Unicode 内容相同，差异仅为编码/字节表示"
                )
                notes.append(f"# encoding-only-reason: {reason} ({reason_label})")
                return (
                    FileChange(
                        rel,
                        "encoding",
                        notes
                        + [
                            f"--- a/{rel}",
                            f"+++ b/{rel}",
                            "# no textual hunks (encoding-only)",
                        ],
                        lines_a=plain_a,
                        lines_b=plain_b,
                        path_a=str(pa),
                        path_b=str(pb),
                        encoding_a=dec_a.encoding,
                        encoding_b=dec_b.encoding,
                        encoding_only=True,
                        encoding_note="\n".join(notes),
                        encoding_reason=reason,
                    ),
                    entry,
                )
            return None, entry

        entry = _entry_both(rel, dec_a, dec_b)
        if lines_equivalent(
            kept_a,
            kept_b,
            rel=rel,
            ignore_whitespace=ignore_whitespace,
            ignore_comments=ignore_comments,
        ):
            return None, entry

        return (
            FileChange(
                rel,
                "modified",
                make_unified_diff_lines(
                    plain_a,
                    plain_b,
                    rel=rel,
                    ignore_whitespace=ignore_whitespace,
                    ignore_comments=ignore_comments,
                ),
                lines_a=plain_a,
                lines_b=plain_b,
                path_a=str(pa),
                path_b=str(pb),
                encoding_a=dec_a.encoding,
                encoding_b=dec_b.encoding,
                encoding_note="\n".join(notes),
            ),
            entry,
        )

    workers = _io_workers()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for rel, (change, entry) in zip(only_a, pool.map(_work_only_a, only_a)):
            changes.append(change)
            if entry is not None:
                encoding_report.append(entry)
            _tick(rel)
        for rel, (change, entry) in zip(only_b, pool.map(_work_only_b, only_b)):
            changes.append(change)
            if entry is not None:
                encoding_report.append(entry)
            _tick(rel)
        for rel, (change, entry) in zip(common, pool.map(_work_common, common)):
            if change is not None:
                changes.append(change)
            if entry is not None:
                encoding_report.append(entry)
            _tick(rel)

    return CompareResult(
        changes=changes,
        only_a=len(only_a),
        only_b=len(only_b),
        common=len(common),
        encoding_report=encoding_report,
    )


def make_unified_diff_lines(
    lines_a: list[str],
    lines_b: list[str],
    *,
    rel: str,
    ignore_whitespace: bool = False,
    ignore_comments: bool = False,
) -> list[str]:
    """
    Build unified diff lines.
    With ignore_comments: match on code keys but emit original text so that
    comment-only *edits* are omitted, while added/removed comment lines still
    appear as +/-.
    """
    a = [line.rstrip("\n") for line in lines_a]
    b = [line.rstrip("\n") for line in lines_b]
    if not ignore_comments and not ignore_whitespace:
        return _strip_diff(
            unified_diff(a, b, fromfile=f"a/{rel}", tofile=f"b/{rel}", n=3, lineterm="\n")
        )

    keys_a = normalize_lines(
        a,
        rel=rel,
        ignore_whitespace=ignore_whitespace,
        ignore_comments=ignore_comments,
    )
    keys_b = normalize_lines(
        b,
        rel=rel,
        ignore_whitespace=ignore_whitespace,
        ignore_comments=ignore_comments,
    )
    if ignore_whitespace:
        keep_a = [i for i, line in enumerate(a) if not _is_blank_line(line)]
        keep_b = [i for i, line in enumerate(b) if not _is_blank_line(line)]
        a = [a[i] for i in keep_a]
        b = [b[i] for i in keep_b]
        keys_a = [keys_a[i] for i in keep_a]
        keys_b = [keys_b[i] for i in keep_b]
    return _unified_diff_by_keys(a, b, keys_a, keys_b, rel=rel)


def _unified_diff_by_keys(
    a: list[str],
    b: list[str],
    keys_a: list[str],
    keys_b: list[str],
    *,
    rel: str,
    n: int = 3,
) -> list[str]:
    """Like difflib.unified_diff, but opcodes come from keys while text is original."""
    matcher = SequenceMatcher(None, keys_a, keys_b)
    groups = list(matcher.get_grouped_opcodes(n))
    if not groups:
        return []

    out: list[str] = [f"--- a/{rel}", f"+++ b/{rel}"]
    for group in groups:
        first_i1 = group[0][1]
        last_i2 = group[-1][2]
        first_j1 = group[0][3]
        last_j2 = group[-1][4]
        out.append(
            f"@@ -{first_i1 + 1},{last_i2 - first_i1} "
            f"+{first_j1 + 1},{last_j2 - first_j1} @@"
        )
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                for line in a[i1:i2]:
                    out.append(f" {line}")
            elif tag == "delete":
                for line in a[i1:i2]:
                    out.append(f"-{line}")
            elif tag == "insert":
                for line in b[j1:j2]:
                    out.append(f"+{line}")
            else:  # replace
                for line in a[i1:i2]:
                    out.append(f"-{line}")
                for line in b[j1:j2]:
                    out.append(f"+{line}")
    return out


def build_unified_report(
    result: CompareResult,
    *,
    ignore_whitespace: bool = False,
    ignore_comments: bool = False,
) -> str:
    parts: list[str] = [
        "# encoding: utf-8",
        "# All file contents were decoded to Unicode before diffing.",
        "# +/- lines are Unicode text, not original bytes. Apply with content match;",
        "# keep or convert target file encoding explicitly after a correct decode.",
        "",
    ]
    for change in result.changes:
        parts.append(f"=== {KIND_LABEL[change.kind]}: {change.rel} ===")
        if change.encoding_note:
            parts.extend(change.encoding_note.splitlines())
        if change.kind == "binary":
            parts.extend(change.diff_lines)
        elif change.kind == "encoding":
            # Notes already include encoding-only markers; avoid duplicating stored lines' notes.
            for line in change.diff_lines:
                if line.startswith("# left:") or line.startswith("# right:"):
                    continue
                if line.startswith("# warning") or line.startswith("# encoding-only"):
                    continue
                parts.append(line)
        elif ignore_comments or ignore_whitespace or not change.diff_lines:
            parts.extend(
                make_unified_diff_lines(
                    change.lines_a,
                    change.lines_b,
                    rel=change.rel,
                    ignore_whitespace=ignore_whitespace,
                    ignore_comments=ignore_comments,
                )
            )
        else:
            parts.extend(change.diff_lines)
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def build_encoding_report(result: CompareResult) -> str:
    lines = [
        "# encoding_report",
        "# format: path | side | encoding | flags | warning",
        "",
    ]
    for entry in result.encoding_report:
        flags: list[str] = []
        if entry.ambiguous:
            flags.append("ambiguous")
        if entry.encoding_only:
            flags.append("encoding-only")
        if entry.encoding_a and entry.encoding_b and entry.encoding_a != entry.encoding_b:
            flags.append("mismatch")
        flag_s = ",".join(flags) if flags else "-"
        warn = entry.warning.replace("\n", " ") if entry.warning else "-"
        lines.append(
            f"{entry.rel} | {entry.side} | {entry.encoding} | {flag_s} | {warn}"
        )
    if len(lines) == 3:
        lines.append("(no text files scanned)")
    return "\n".join(lines) + "\n"
