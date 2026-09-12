"""Tier-1 comment stripping by language / file extension (heuristic)."""

from __future__ import annotations

import re

from language import comment_style_for_language, detect_language


def comment_style_for(path_or_rel: str, sample: str = "") -> str:
    language = detect_language(path_or_rel, sample)
    style = comment_style_for_language(language)
    # Unknown extensions (or mislabeled text) that clearly use C-like comments.
    if style == "none" and sample:
        if "//" in sample or "/*" in sample:
            return "c"
        if re.search(r"^\s*#\s", sample, re.M) and not sample.lstrip().startswith("#!"):
            # e.g. linker/script snippets with # comments
            return "hash"
    return style


def _rstrip_with_map(text: str, index_map: list[int]) -> tuple[str, list[int]]:
    end = len(text)
    while end > 0 and text[end - 1].isspace():
        end -= 1
    return text[:end], index_map[:end]


def _strip_line_comment_mapped(line: str, marker: str) -> tuple[str, list[int]]:
    if not marker:
        return line, list(range(len(line)))
    in_single = False
    in_double = False
    in_back = False
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if ch == "\\" and (in_single or in_double or in_back):
            i += 2
            continue
        if not in_double and not in_back and ch == "'":
            in_single = not in_single
            i += 1
            continue
        if not in_single and not in_back and ch == '"':
            in_double = not in_double
            i += 1
            continue
        if not in_single and not in_double and ch == "`":
            in_back = not in_back
            i += 1
            continue
        if (
            not in_single
            and not in_double
            and not in_back
            and line.startswith(marker, i)
        ):
            return _rstrip_with_map(line[:i], list(range(i)))
        i += 1
    return line, list(range(n))


def _strip_c_like_line_mapped(
    line: str, line_marker: str, in_block: bool
) -> tuple[str, list[int], bool]:
    chars: list[str] = []
    index_map: list[int] = []
    i = 0
    n = len(line)
    in_single = in_double = in_back = False
    while i < n:
        if in_block:
            end = line.find("*/", i)
            if end < 0:
                text, index_map = _rstrip_with_map("".join(chars), index_map)
                return text, index_map, True
            i = end + 2
            in_block = False
            continue

        ch = line[i]
        if ch == "\\" and (in_single or in_double or in_back):
            if i + 1 < n:
                chars.extend((line[i], line[i + 1]))
                index_map.extend((i, i + 1))
                i += 2
            else:
                chars.append(ch)
                index_map.append(i)
                i += 1
            continue
        if not in_double and not in_back and ch == "'":
            in_single = not in_single
            chars.append(ch)
            index_map.append(i)
            i += 1
            continue
        if not in_single and not in_back and ch == '"':
            in_double = not in_double
            chars.append(ch)
            index_map.append(i)
            i += 1
            continue
        if not in_single and not in_double and ch == "`":
            in_back = not in_back
            chars.append(ch)
            index_map.append(i)
            i += 1
            continue

        if not in_single and not in_double and not in_back:
            if line.startswith("/*", i):
                in_block = True
                i += 2
                continue
            if line_marker and line.startswith(line_marker, i):
                break

        chars.append(ch)
        index_map.append(i)
        i += 1

    text, index_map = _rstrip_with_map("".join(chars), index_map)
    return text, index_map, in_block


def _strip_html_line_mapped(line: str, in_block: bool) -> tuple[str, list[int], bool]:
    chars: list[str] = []
    index_map: list[int] = []
    i = 0
    n = len(line)
    while i < n:
        if in_block:
            end = line.find("-->", i)
            if end < 0:
                text, index_map = _rstrip_with_map("".join(chars), index_map)
                return text, index_map, True
            i = end + 3
            in_block = False
            continue
        start = line.find("<!--", i)
        if start < 0:
            while i < n:
                chars.append(line[i])
                index_map.append(i)
                i += 1
            break
        while i < start:
            chars.append(line[i])
            index_map.append(i)
            i += 1
        in_block = True
        i = start + 4

    text, index_map = _rstrip_with_map("".join(chars), index_map)
    return text, index_map, in_block


def strip_line_with_index_map(line: str, style: str) -> tuple[str, list[int]]:
    """
    Strip comments from one line.
    Returns (stripped_text, index_map) where index_map[i] is the original
    character index of stripped_text[i]. Comment regions are omitted from the map,
    so inline diffs on stripped text can be projected onto code-only spans.
    """
    if style == "none":
        return line, list(range(len(line)))
    if style == "hash":
        return _strip_line_comment_mapped(line, "#")
    if style == "c":
        text, index_map, _ = _strip_c_like_line_mapped(line, "//", False)
        return text, index_map
    if style == "sql":
        text, index_map, _ = _strip_c_like_line_mapped(line, "--", False)
        return text, index_map
    if style == "html":
        text, index_map, _ = _strip_html_line_mapped(line, False)
        return text, index_map
    return line, list(range(len(line)))


def _strip_c_like(lines: list[str], line_marker: str) -> list[str]:
    out: list[str] = []
    in_block = False
    for line in lines:
        text, _, in_block = _strip_c_like_line_mapped(line, line_marker, in_block)
        out.append(text)
    return out


def _strip_html(lines: list[str]) -> list[str]:
    out: list[str] = []
    in_block = False
    for line in lines:
        text, _, in_block = _strip_html_line_mapped(line, in_block)
        out.append(text)
    return out


def strip_comments_from_lines(lines: list[str], style: str) -> list[str]:
    if style == "none" or not lines:
        return list(lines)
    if style == "hash":
        return [_strip_line_comment_mapped(line, "#")[0] for line in lines]
    if style == "c":
        return _strip_c_like(lines, "//")
    if style == "sql":
        return _strip_c_like(lines, "--")
    if style == "html":
        return _strip_html(lines)
    return list(lines)


def is_insignificant_line(line: str, style: str) -> bool:
    """True if line is empty after comment strip (comment-only / blank)."""
    if style == "none":
        return line.strip() == ""
    stripped, _ = strip_line_with_index_map(line, style)
    return stripped.strip() == ""


def looks_like_comment_line(line: str, style: str) -> bool:
    """
    Heuristic for a line that is only comment / comment continuation.
    Used to avoid shift artifacts (e.g. closing '*/' marked deleted when the
    block only moved due to inserts above). Does not require block state.
    """
    s = line.strip()
    if not s:
        return True
    if style in ("c", "sql"):
        if s.startswith("//") or s.startswith("/*"):
            return True
        if s.startswith("--") and style == "sql":
            return True
        # Block-comment continuation or closer: " * ...", "*/", "**/", etc.
        if s.startswith("*") or s == "*/" or s.endswith("*/"):
            return True
    if style == "hash" and s.startswith("#"):
        return True
    if style == "html" and ("<!--" in s or "-->" in s):
        return True
    return is_insignificant_line(line, style)


def map_spans_to_original(
    spans: list[tuple[int, int]], index_map: list[int]
) -> list[tuple[int, int]]:
    """Map half-open spans in stripped space onto original line indices."""
    if not spans or not index_map:
        return []
    mapped: list[tuple[int, int]] = []
    n = len(index_map)
    for start, end in spans:
        if start >= end:
            continue
        s = max(0, min(start, n - 1))
        e = max(0, min(end, n))
        if s >= e:
            continue
        # [s, e) in stripped -> [map[s], map[e-1] + 1) in original
        orig_start = index_map[s]
        orig_end = index_map[e - 1] + 1
        if orig_start < orig_end:
            mapped.append((orig_start, orig_end))
    return mapped
