"""Read text files with reliable encoding detection (no mojibake in diffs)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

_ALLOWED_HINTS = {
    "utf-8",
    "utf_8",
    "utf8",
    "utf-8-sig",
    "utf_8_sig",
    "gb18030",
    "gbk",
    "gb2312",
    "gb_2312",
    "big5",
    "shift_jis",
    "shift-jis",
    "utf-16",
    "utf_16",
    "utf-16-le",
    "utf_16_le",
    "utf-16-be",
    "utf_16_be",
}


@dataclass
class DecodeResult:
    text: str
    encoding: str
    ambiguous: bool = False
    candidates: list[str] = field(default_factory=list)
    warning: str = ""


def _cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    cjk = 0
    total = 0
    for ch in text:
        if ch.isspace():
            continue
        total += 1
        o = ord(ch)
        if (
            0x4E00 <= o <= 0x9FFF
            or 0x3400 <= o <= 0x4DBF
            or 0xF900 <= o <= 0xFAFF
            or 0x3000 <= o <= 0x303F
            or 0xFF00 <= o <= 0xFFEF
        ):
            cjk += 1
    return (cjk / total) if total else 0.0


def _try_strict(raw: bytes, encoding: str) -> str | None:
    try:
        return raw.decode(encoding, errors="strict")
    except UnicodeDecodeError:
        return None


def _score_text(text: str) -> float:
    if not text:
        return 0.0
    bad = sum(1 for ch in text if ch == "\ufffd" or (ord(ch) < 32 and ch not in "\t\n\r"))
    cjk = _cjk_ratio(text)
    return cjk * 10.0 - bad * 5.0 + min(len(text), 10_000) / 10_000.0


def _normalize_hint_name(name: str) -> str | None:
    key = name.replace("-", "_").lower()
    aliases = {
        "utf_8": "utf-8",
        "utf8": "utf-8",
        "utf_8_sig": "utf-8-sig",
        "gb2312": "gbk",
        "gb_2312": "gbk",
        "gb18030": "gb18030",
        "gbk": "gbk",
        "big5": "big5",
        "shift_jis": "shift_jis",
    }
    if key not in _ALLOWED_HINTS and name.lower() not in _ALLOWED_HINTS:
        return None
    return aliases.get(key, aliases.get(name.lower(), name))


def detect_and_decode(raw: bytes) -> DecodeResult:
    """
    Decode bytes to Unicode for comparison.

    - BOM handled strictly.
    - Prefer strict UTF-8, then GB18030/GBK.
    - Both OK but different text: prefer UTF-8 unless CJK strongly favors GB*.
    - Never use errors=replace for diff content.
    """
    if not raw:
        return DecodeResult("", "utf-8")

    if raw.startswith(b"\xff\xfe"):
        text = _try_strict(raw, "utf-16-le")
        if text is not None:
            return DecodeResult(text, "utf-16-le")
    if raw.startswith(b"\xfe\xff"):
        text = _try_strict(raw, "utf-16-be")
        if text is not None:
            return DecodeResult(text, "utf-16-be")
    if raw.startswith(b"\xef\xbb\xbf"):
        text = _try_strict(raw, "utf-8-sig")
        if text is not None:
            return DecodeResult(text, "utf-8-sig")

    candidates: list[tuple[str, str, float]] = []

    utf8 = _try_strict(raw, "utf-8")
    if utf8 is not None:
        candidates.append(("utf-8", utf8, _score_text(utf8)))

    for enc in ("gb18030", "gbk"):
        text = _try_strict(raw, enc)
        if text is not None:
            candidates.append((enc, text, _score_text(text)))

    try:
        from charset_normalizer import from_bytes

        match = from_bytes(raw).best()
        if match is not None and match.encoding:
            enc = _normalize_hint_name(match.encoding)
            if enc and not any(c[0] == enc for c in candidates):
                text = _try_strict(raw, enc)
                if text is not None:
                    candidates.append((enc, text, _score_text(text)))
    except Exception:
        pass

    if not candidates:
        return DecodeResult(
            raw.decode("latin-1"),
            "latin-1",
            ambiguous=True,
            warning="无法可靠识别编码，已用 latin-1 按字节映射（请人工确认）",
        )

    utf = next((c for c in candidates if c[0] in {"utf-8", "utf-8-sig"}), None)
    gb = next((c for c in candidates if c[0] in {"gb18030", "gbk"}), None)

    if utf and gb:
        utf_cjk, gb_cjk = _cjk_ratio(utf[1]), _cjk_ratio(gb[1])
        if utf[1] == gb[1]:
            return DecodeResult(utf[1], utf[0], candidates=[utf[0], gb[0]])
        if gb_cjk >= 0.08 and utf_cjk < 0.02 and gb_cjk > utf_cjk + 0.05:
            return DecodeResult(
                gb[1],
                gb[0],
                ambiguous=True,
                candidates=[utf[0], gb[0]],
                warning=f"UTF-8 与 {gb[0]} 均可解码且文本不同，按 CJK 比例选用 {gb[0]}",
            )
        return DecodeResult(
            utf[1],
            utf[0],
            ambiguous=True,
            candidates=[utf[0], gb[0]],
            warning=f"UTF-8 与 {gb[0]} 均可解码且文本不同，优先 UTF-8",
        )

    gb_only = [c for c in candidates if c[0] in {"gb18030", "gbk"}]
    if gb_only and all(c[1] == gb_only[0][1] for c in gb_only) and utf is None:
        pick = next((c for c in gb_only if c[0] == "gb18030"), gb_only[0])
        return DecodeResult(pick[1], pick[0], candidates=[c[0] for c in gb_only])

    best = max(candidates, key=lambda c: c[2])
    others = [c[0] for c in candidates if c[0] != best[0]]
    same_text = all(c[1] == best[1] for c in candidates)
    names = {best[0], *others}
    if names <= {"gb18030", "gbk"} and same_text:
        return DecodeResult(best[1], best[0], candidates=[best[0], *others])
    return DecodeResult(
        best[1],
        best[0],
        ambiguous=bool(others) and not same_text,
        candidates=[best[0], *others],
        warning=("多个编码均可解码且文本不同，已选 " + best[0])
        if others and not same_text
        else "",
    )


def read_text_auto(path: Path) -> DecodeResult:
    return detect_and_decode(path.read_bytes())


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")
