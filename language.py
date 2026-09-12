"""Detect source language from path and a short content sample."""

from __future__ import annotations

import re
from pathlib import Path

# language id -> comment style used by comments.py
LANG_COMMENT_STYLE: dict[str, str] = {
    "python": "hash",
    "shell": "hash",
    "ruby": "hash",
    "perl": "hash",
    "r": "hash",
    "yaml": "hash",
    "toml": "hash",
    "gitignore": "hash",
    "javascript": "c",
    "typescript": "c",
    "java": "c",
    "kotlin": "c",
    "c": "c",
    "cpp": "c",
    "csharp": "c",
    "go": "c",
    "rust": "c",
    "swift": "c",
    "css": "c",
    "scss": "c",
    "php": "c",
    "vue": "c",
    "jsonc": "c",
    "sql": "sql",
    "html": "html",
    "xml": "html",
    "markdown": "none",
    "json": "none",
    "text": "none",
}

EXT_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".pyw": "python",
    ".pyi": "python",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".rb": "ruby",
    ".rake": "ruby",
    ".pl": "perl",
    ".pm": "perl",
    ".r": "r",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".gitignore": "gitignore",
    ".dockerignore": "gitignore",
    ".editorconfig": "gitignore",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".c": "c",
    ".h": "c",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".hp": "cpp",
    ".hxx": "cpp",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".s": "c",
    ".S": "c",
    ".asm": "c",
    ".inc": "c",
    ".ld": "c",
    ".lds": "c",
    ".cs": "csharp",
    ".go": "go",
    ".rs": "rust",
    ".swift": "swift",
    ".css": "css",
    ".scss": "scss",
    ".less": "scss",
    ".sass": "scss",
    ".php": "php",
    ".vue": "vue",
    ".jsonc": "jsonc",
    ".sql": "sql",
    ".html": "html",
    ".htm": "html",
    ".xml": "xml",
    ".svg": "xml",
    ".xhtml": "xml",
    ".md": "markdown",
    ".markdown": "markdown",
    ".json": "json",
    ".txt": "text",
    ".ino": "cpp",
}

_SHEBANG_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^#!.*\bpython[0-9.]*\b", re.I), "python"),
    (re.compile(r"^#!.*\b(bash|sh|zsh|dash)\b", re.I), "shell"),
    (re.compile(r"^#!.*\bruby\b", re.I), "ruby"),
    (re.compile(r"^#!.*\bperl\b", re.I), "perl"),
    (re.compile(r"^#!.*\bnode\b", re.I), "javascript"),
]

_CONTENT_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^\s*#include\s*[<\"]", re.M), "c"),
    (re.compile(r"^\s*#define\b", re.M), "c"),
    (re.compile(r"^\s*#if(n?def)?\b", re.M), "c"),
    (re.compile(r"^\s*package\s+\w+\s*;", re.M), "java"),
    (re.compile(r"^\s*fn\s+main\s*\(", re.M), "rust"),
    (re.compile(r"^\s*package\s+main\b", re.M), "go"),
    (re.compile(r"^\s*<(!DOCTYPE\s+)?html\b", re.I | re.M), "html"),
    (re.compile(r"^\s*<\?xml\b", re.I | re.M), "xml"),
    (re.compile(r"^\s*<\?php\b", re.I | re.M), "php"),
    (re.compile(r"^\s*(import\s+|from\s+\w+\s+import\s+)", re.M), "python"),
    (re.compile(r"^\s*(using\s+System\b|namespace\s+\w+)", re.M), "csharp"),
]


def detect_language(path_or_rel: str, sample: str = "") -> str:
    """Return a language id; prefer extension, then shebang/content sniff."""
    name = Path(path_or_rel).name
    suffix = Path(path_or_rel).suffix.lower()
    if name == "Makefile" or name.lower() == "makefile":
        return "shell"
    if name == "Dockerfile" or name.lower().startswith("dockerfile"):
        return "shell"
    if suffix in EXT_LANGUAGE:
        return EXT_LANGUAGE[suffix]

    head = (sample or "")[:4000]
    if head:
        first = head.lstrip("\ufeff").splitlines()[:1]
        line0 = first[0] if first else ""
        for pattern, lang in _SHEBANG_RULES:
            if pattern.search(line0):
                return lang
        for pattern, lang in _CONTENT_RULES:
            if pattern.search(head):
                return lang
    return "text"


def comment_style_for_language(language: str) -> str:
    return LANG_COMMENT_STYLE.get(language, "none")
