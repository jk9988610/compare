#!/usr/bin/env python3
"""Tkinter GUI for directory compare."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import tkinter as tk
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from engine import (
    AlignedRow,
    CompareResult,
    FileChange,
    align_lines,
    change_row_indices,
    compute_diff,
)
from encodingutil import detect_and_decode
from export import export_html_report, export_unified_diff
from language import detect_language
from cache import cache_fingerprint, load_compare_cache, save_compare_cache
from settings import load_settings, save_settings

# Deep dark chrome (less mid-gray wash)
BG = "#0d0d0d"
PANEL = "#121212"
TITLE_BG = "#161616"
FOOTER_BG = "#101010"
INK = "#e6e6e6"
MUTED = "#8a8a8a"
ACCENT = "#0a84ff"
LINE = "#2a2a2a"
ENTRY = "#1a1a1a"
DIFF_BG = "#0a0a0a"
HOVER = "#252525"
CLOSE_HOVER = "#e81123"


def _normalize_folder(folder: str) -> str:
    return folder.strip().replace("\\", "/").strip("/")


def _normalize_type_token(tok: str) -> tuple[bool, str]:
    """Return (is_negation, glob). Bare 'o' / '.o' become '*.o'."""
    tok = tok.strip()
    neg = tok.startswith("!")
    body = tok[1:].strip() if neg else tok
    if not body:
        return neg, ""
    if any(ch in body for ch in "*?[]/") or body.startswith("*."):
        spec = body
    elif body.startswith("."):
        spec = f"*{body}"
    else:
        spec = f"*.{body}"
    return neg, spec


def compile_ignore_rule(folder: str, types: str) -> list[str]:
    """Turn UI rule into gitignore-style patterns."""
    folder = _normalize_folder(folder)
    tokens = [t for t in re.split(r"[,;\s]+", types.strip()) if t.strip()]
    if not tokens:
        if not folder:
            return []
        return [f"{folder}/"]
    out: list[str] = []
    for tok in tokens:
        neg, spec = _normalize_type_token(tok)
        if not spec:
            continue
        if folder:
            pat = f"{folder}/**/{spec}"
        else:
            pat = spec if spec.startswith("**/") else f"**/{spec}"
        if neg:
            pat = "!" + pat.lstrip("!")
        out.append(pat)
    return out


def compile_ignore_rules(rules: list[dict[str, str]]) -> list[str]:
    patterns: list[str] = []
    for rule in rules:
        patterns.extend(
            compile_ignore_rule(str(rule.get("folder", "")), str(rule.get("types", "")))
        )
    return patterns


def rules_from_patterns(patterns: list[str]) -> list[dict[str, str]]:
    """Best-effort migrate raw gitignore lines into simple folder/types rules."""
    rules: list[dict[str, str]] = []
    for pat in patterns:
        p = pat.strip()
        if not p or p.startswith("#"):
            continue
        if p.endswith("/") and "*" not in p and "?" not in p:
            rules.append({"folder": p.rstrip("/"), "types": ""})
            continue
        m = re.match(r"^(.+)/\*\*/(.+)$", p)
        if m:
            rules.append({"folder": m.group(1), "types": m.group(2)})
            continue
        if p.startswith("**/"):
            rules.append({"folder": "", "types": p[3:]})
            continue
        rules.append({"folder": "", "types": p})
    return rules


def rule_summary(folder: str, types: str) -> str:
    folder = _normalize_folder(folder) or "任意目录"
    types = types.strip() or "全部类型"
    return f"忽略 {folder} 下的 {types} 文件"


def scan_exts_under(folder: str, roots: list[Path]) -> list[str]:
    """Extensions found under a relative folder (empty = whole trees)."""
    folder = _normalize_folder(folder)
    exts: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        base = root.resolve() if not folder else (root.resolve() / folder)
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d != ".git" and not d.startswith(".")]
            for name in filenames:
                suf = Path(name).suffix.lower()
                if suf and len(suf) <= 32:
                    exts.add(f"*{suf}")
    return sorted(exts, key=str.lower)


def rel_path_under_roots(path: Path, roots: list[Path]) -> str | None:
    """Return posix relative path if path is under a root; '' if path is a root."""
    path = path.resolve()
    for root in roots:
        if not root.is_dir():
            continue
        root = root.resolve()
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if rel == Path("."):
            return ""
        return rel.as_posix()
    return None


FOLDER_ANY = "（任意目录）"
TYPE_ALL = "（全部类型）"
_RENDER_CACHE_MAX = 20


@dataclass
class _RenderSnapshot:
    left: str
    right: str
    left_tags: list[tuple[str, int, int]]
    right_tags: list[tuple[str, int, int]]
    change_rows: list[int]
    rows: list[AlignedRow]


class SearchCombo(ttk.Frame):
    """Combobox that filters its list as the user types."""

    def __init__(
        self,
        parent: tk.Misc,
        values: list[str],
        *,
        textvariable: tk.StringVar | None = None,
        width: int = 18,
        on_change=None,
    ) -> None:
        super().__init__(parent)
        self._all = list(values)
        self._on_change = on_change
        self.var = textvariable or tk.StringVar()
        self.combo = ttk.Combobox(self, textvariable=self.var, values=self._all, width=width)
        self.combo.pack(fill=tk.X, expand=True)
        self.combo.bind("<KeyRelease>", self._on_key, add="+")
        self.combo.bind("<<ComboboxSelected>>", self._emit, add="+")
        self.combo.bind("<FocusOut>", self._emit, add="+")

    def set_values(self, values: list[str]) -> None:
        current = self.var.get()
        self._all = list(values)
        if current and current not in self._all:
            self._all = [current, *self._all]
        self.combo.configure(values=self._all)

    def _on_key(self, event) -> None:
        if event.keysym in {
            "Return",
            "Up",
            "Down",
            "Left",
            "Right",
            "Escape",
            "Tab",
            "Shift_L",
            "Shift_R",
            "Control_L",
            "Control_R",
        }:
            return
        q = self.var.get().lower()
        filtered = [v for v in self._all if q in v.lower()] if q else list(self._all)
        self.combo.configure(values=filtered or list(self._all))
        # Filtering only — commit on select / focus out.

    def _emit(self, _event=None) -> None:
        if self._on_change is not None:
            self._on_change()


def _pick_font(root: tk.Tk, candidates: list[str], size: int) -> tuple[str, int]:
    import tkinter.font as tkfont

    available = set(tkfont.families(root))
    for name in candidates:
        if name in available:
            return (name, size)
    return (candidates[-1], size)


def _ui_font(root: tk.Tk) -> tuple[str, int]:
    names = ["Microsoft YaHei UI", "PingFang SC", "Noto Sans CJK SC", "Segoe UI"]
    size = 12 if sys.platform == "darwin" else 10
    return _pick_font(root, names, size)


def _mono_font(root: tk.Tk) -> tuple[str, int]:
    names = ["Cascadia Mono", "Consolas", "Menlo", "DejaVu Sans Mono"]
    size = 12 if sys.platform == "darwin" else 10
    return _pick_font(root, names, size)


class _Tooltip:
    """Hover tip showing full directory path."""

    def __init__(self, widget: tk.Widget, text: str = "") -> None:
        self.widget = widget
        self.text = text
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, _event=None) -> None:
        if not self.text or self._tip is not None:
            return
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        tip.attributes("-topmost", True)
        x = self.widget.winfo_rootx() + 8
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        tip.wm_geometry(f"+{x}+{y}")
        label = tk.Label(
            tip,
            text=self.text,
            justify=tk.LEFT,
            background="#1c1c1c",
            foreground=INK,
            relief=tk.SOLID,
            borderwidth=1,
            font=("Microsoft YaHei UI", 9),
            padx=8,
            pady=4,
        )
        label.pack()
        self._tip = tip

    def _hide(self, _event=None) -> None:
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


class CompareApp(tk.Tk):
    def __init__(self, dir_a: Path | None = None, dir_b: Path | None = None) -> None:
        super().__init__()
        self.title("目录对比")
        self.minsize(960, 560)
        self.configure(bg=BG)

        self._all_changes: list[FileChange] = []
        self._filtered: list[FileChange] = []
        self._last_result: CompareResult | None = None
        self._busy = False
        self._aligned: list[AlignedRow] = []
        self._change_rows: list[int] = []
        self._change_cursor = -1
        self._syncing_scroll = False
        self._current_change: FileChange | None = None
        self._align_cache: dict[tuple[str, bool, bool], list[AlignedRow]] = {}
        self._render_cache: OrderedDict[tuple, _RenderSnapshot] = OrderedDict()
        self._show_gen = 0
        self._wrap_resize_after: str | None = None
        self._last_diff_size: tuple[int, int] = (0, 0)
        self._mono_px = 8
        self._ignore_patterns: list[str] = []
        self._ignore_rules: list[dict[str, str]] = []
        self._sidebar_visible = True
        self._maximized = False
        self._restore_geometry = "1200x760"
        self._drag_offset: tuple[int, int] | None = None
        self._custom_chrome = sys.platform == "win32"
        self._taskbar_refreshing = False
        self._busy_widgets: list[tk.Widget] = []

        self._ui = _ui_font(self)
        self._mono = _mono_font(self)

        cfg = load_settings()
        rules = cfg.get("ignore_rules") or []
        if isinstance(rules, list) and rules:
            self._ignore_rules = [
                {"folder": str(r.get("folder", "")), "types": str(r.get("types", ""))}
                for r in rules
                if isinstance(r, dict)
            ]
            self._ignore_patterns = compile_ignore_rules(self._ignore_rules)
        else:
            pats = cfg.get("ignore_patterns") or []
            self._ignore_patterns = [str(p) for p in pats] if isinstance(pats, list) else []
            self._ignore_rules = rules_from_patterns(self._ignore_patterns)
            if self._ignore_rules:
                self._ignore_patterns = compile_ignore_rules(self._ignore_rules)
        self._sidebar_visible = bool(cfg.get("sidebar_visible", True))

        if self._custom_chrome:
            self.overrideredirect(True)
            self.bind("<Map>", self._on_map_restore_chrome)

        self._setup_style()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind_all("<F7>", lambda e: self._goto_change(1))
        self.bind_all("<Shift-F7>", lambda e: self._goto_change(-1))

        try:
            geo = str(cfg.get("geometry") or "1200x760")
            self.geometry(geo)
            self._restore_geometry = geo
        except tk.TclError:
            pass

        if dir_a:
            self.var_a.set(str(dir_a.resolve()))
        elif cfg.get("dir_a"):
            self.var_a.set(str(cfg["dir_a"]))
        if dir_b:
            self.var_b.set(str(dir_b.resolve()))
        elif cfg.get("dir_b"):
            self.var_b.set(str(cfg["dir_b"]))

        if "ignore_whitespace" in cfg:
            self.var_ignore_ws.set(bool(cfg["ignore_whitespace"]))
        if "ignore_comments" in cfg:
            self.var_ignore_comments.set(bool(cfg["ignore_comments"]))
        if "word_wrap" in cfg:
            self.var_wrap.set(bool(cfg["word_wrap"]))
            self._apply_wrap()
        if "show_encoding_only" in cfg:
            self.var_show_encoding_only.set(bool(cfg["show_encoding_only"]))

        if not self._sidebar_visible:
            self.after_idle(lambda: self._set_sidebar_visible(False, persist=False))

        if self._custom_chrome:
            self.after(80, self._ensure_taskbar_icon)

        a = self.var_a.get().strip()
        b = self.var_b.get().strip()
        if a and b and Path(a).is_dir() and Path(b).is_dir():
            self.after(120, self._try_restore_or_compare)

    def _setup_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        self.tk_setPalette(
            background=BG,
            foreground=INK,
            activeBackground=LINE,
            activeForeground=INK,
            highlightBackground=BG,
            highlightColor=ACCENT,
            insertBackground=INK,
            selectBackground=ACCENT,
            selectForeground="#ffffff",
        )
        ui = self._ui
        style.configure(".", background=BG, foreground=INK, font=ui)
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("Title.TFrame", background=TITLE_BG)
        style.configure("TLabel", background=BG, foreground=INK, font=ui)
        style.configure("Panel.TLabel", background=PANEL, foreground=INK, font=ui)
        style.configure("Title.TLabel", background=TITLE_BG, foreground=INK, font=ui)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=ui)
        style.configure("Status.TLabel", background=FOOTER_BG, foreground=MUTED, font=ui)
        style.configure("Footer.TFrame", background=FOOTER_BG)
        style.configure("Footer.TLabel", background=FOOTER_BG, foreground=MUTED, font=ui)
        style.configure(
            "Footer.TCheckbutton",
            background=FOOTER_BG,
            foreground=INK,
            font=ui,
            focuscolor=FOOTER_BG,
        )
        style.map("Footer.TCheckbutton", background=[("active", FOOTER_BG)])
        style.configure(
            "TEntry",
            fieldbackground=ENTRY,
            foreground=INK,
            background=ENTRY,
            insertcolor=INK,
            bordercolor=LINE,
            lightcolor=LINE,
            darkcolor=LINE,
        )
        style.map("TEntry", fieldbackground=[("focus", "#222222")])
        style.configure(
            "TCombobox",
            fieldbackground=ENTRY,
            foreground=INK,
            background=ENTRY,
            arrowcolor=INK,
            bordercolor=LINE,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", ENTRY)],
            selectbackground=[("readonly", ENTRY)],
            selectforeground=[("readonly", INK)],
        )
        style.configure(
            "Accent.TButton",
            background=ACCENT,
            foreground="#ffffff",
            font=ui,
            padding=(14, 5),
            bordercolor=ACCENT,
        )
        style.map(
            "Accent.TButton",
            background=[("active", "#1a7aeb"), ("disabled", "#1a3048")],
            foreground=[("disabled", MUTED)],
        )
        style.configure(
            "TButton",
            font=ui,
            padding=(8, 4),
            background=LINE,
            foreground=INK,
            bordercolor=LINE,
        )
        style.map("TButton", background=[("active", HOVER), ("pressed", "#141414")])
        style.configure("TCheckbutton", background=BG, foreground=INK, font=ui, focuscolor=BG)
        style.map("TCheckbutton", background=[("active", BG)])
        style.configure(
            "Treeview",
            background=PANEL,
            fieldbackground=PANEL,
            foreground=INK,
            font=ui,
            rowheight=26,
            borderwidth=0,
            bordercolor=LINE,
        )
        style.configure(
            "Treeview.Heading",
            font=ui,
            background="#1c1c1c",
            foreground=INK,
            bordercolor=LINE,
            relief="flat",
        )
        style.map(
            "Treeview",
            background=[("selected", ACCENT)],
            foreground=[("selected", "#ffffff")],
        )
        style.map("Treeview.Heading", background=[("active", HOVER)])
        style.configure("TPanedwindow", background=BG)
        style.configure(
            "TScrollbar",
            background="#222222",
            troughcolor=BG,
            arrowcolor=INK,
            bordercolor=LINE,
        )
        style.map("TScrollbar", background=[("active", "#333333")])
        style.configure(
            "Footer.Horizontal.TProgressbar",
            troughcolor="#1a1a1a",
            background=ACCENT,
            bordercolor=LINE,
            lightcolor=ACCENT,
            darkcolor=ACCENT,
        )

    def _build(self) -> None:
        self.var_a = tk.StringVar()
        self.var_b = tk.StringVar()
        self.var_ignore_ws = tk.BooleanVar(value=False)
        self.var_ignore_comments = tk.BooleanVar(value=False)
        self.var_wrap = tk.BooleanVar(value=True)
        self.var_show_encoding_only = tk.BooleanVar(value=False)
        self.var_query = tk.StringVar()

        if self._custom_chrome:
            self._build_title_bar()
        else:
            self._build_top_toolbar()

        # Footer first so body expands between title and footer
        self._build_footer()

        body = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True, padx=6, pady=0)
        self._body = body

        left = ttk.Frame(body, style="Panel.TFrame")
        right = ttk.Frame(body, style="Panel.TFrame")
        self._left_pane = left
        self._right_pane = right
        body.add(left, weight=1)
        body.add(right, weight=4)

        filt = ttk.Frame(left, style="Panel.TFrame")
        filt.pack(fill=tk.X, padx=6, pady=(6, 4))
        ttk.Entry(filt, textvariable=self.var_query).pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.var_query.trace_add("write", lambda *_: self._apply_filter())

        tree_wrap = ttk.Frame(left, style="Panel.TFrame")
        tree_wrap.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 6))
        scroll_y = ttk.Scrollbar(tree_wrap)
        scroll_y.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree = ttk.Treeview(
            tree_wrap,
            columns=("name",),
            show="headings",
            yscrollcommand=scroll_y.set,
            selectmode="browse",
        )
        self.tree.heading("name", text="文件名")
        self.tree.column("name", width=160, stretch=True, anchor=tk.W)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll_y.config(command=self.tree.yview)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Button-3>", self._on_tree_menu)
        self.tree.bind("<Motion>", self._on_tree_motion)
        self._tree_tip = _Tooltip(self.tree, "")
        self._tree_tip_row: str | None = None

        panes = ttk.Frame(right, style="Panel.TFrame")
        panes.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        panes.columnconfigure(0, weight=1)
        panes.columnconfigure(1, weight=1)
        panes.rowconfigure(0, weight=1)

        self.left_text = self._make_diff_text(panes)
        self.right_text = self._make_diff_text(panes)
        self.left_text.grid(row=0, column=0, sticky="nsew")
        self.right_text.grid(row=0, column=1, sticky="nsew", padx=(4, 0))

        # Diff overview (change preview strip); scroll via mouse wheel / click.
        self.overview = tk.Canvas(
            panes,
            width=12,
            bg="#0a0a0a",
            highlightthickness=0,
            borderwidth=0,
            cursor="sb_v_double_arrow",
        )
        self.overview.grid(row=0, column=2, sticky="ns", padx=(4, 0))
        self.left_text.configure(yscrollcommand=self._on_left_yscroll)
        self.right_text.configure(yscrollcommand=self._on_right_yscroll)

        for widget in (self.left_text, self.right_text, self.overview):
            widget.bind("<MouseWheel>", self._on_mousewheel)
            widget.bind("<Button-4>", self._on_mousewheel)
            widget.bind("<Button-5>", self._on_mousewheel)
        for widget in (self.left_text, self.right_text):
            widget.bind("<Configure>", self._on_diff_configure)
            # Middle-click paste on Text can desync the two panes; ignore it.
            widget.bind("<Button-2>", lambda _e: "break")
        self.overview.bind("<Configure>", lambda _e: self._draw_overview())
        self.overview.bind("<Button-1>", self._on_overview_click)
        self.overview.bind("<B1-Motion>", self._on_overview_click)
        self._tip_overview = _Tooltip(self.overview, "差异预览 · 点击跳转 · 滚轮同步滚动")

        try:
            import tkinter.font as tkfont

            self._mono_px = max(1, int(tkfont.Font(font=self._mono).measure("0")))
        except Exception:
            self._mono_px = 8

        self._menu = tk.Menu(self, tearoff=0, bg=PANEL, fg=INK, activebackground=ACCENT)
        self._menu.add_command(label="复制路径", command=self._copy_path)
        self._menu.add_command(label="打开文件", command=self._open_file)
        self._menu.add_command(label="在资源管理器中显示", command=self._reveal_file)

    def _build_action_cluster(self, parent: tk.Misc, *, chrome: bool) -> None:
        """Shared 旧侧/新侧/对比/导出 buttons for title bar or top toolbar."""
        gap = {"padx": (0, 4)} if chrome else {"padx": (0, 4)}

        self.btn_a = self._chrome_btn(
            parent, "旧侧", lambda: self._browse(self.var_a), chrome=chrome
        )
        self.btn_a.pack(side=tk.LEFT, **gap)
        self.btn_b = self._chrome_btn(
            parent, "新侧", lambda: self._browse(self.var_b), chrome=chrome
        )
        self.btn_b.pack(side=tk.LEFT, **gap)
        self._chrome_btn(parent, "交换", self._swap_dirs, chrome=chrome).pack(
            side=tk.LEFT, **gap
        )
        self.btn_compare = self._chrome_btn(
            parent, "运行", self._on_compare, chrome=chrome
        )
        self.btn_compare.pack(side=tk.LEFT, **gap)
        self._chrome_btn(
            parent,
            "编码检查",
            self._encoding_prepare,
            chrome=chrome,
            tip="对当前旧侧/新侧：内嵌 encoding_unify 检查，可统一为 UTF-8 无 BOM + LF",
        ).pack(side=tk.LEFT, **gap)
        self._chrome_btn(parent, "规则", self._edit_ignore_patterns, chrome=chrome).pack(
            side=tk.LEFT, **gap
        )
        self.btn_export_diff = self._chrome_btn(
            parent, "Diff", self._export_diff, chrome=chrome
        )
        self.btn_export_html = self._chrome_btn(
            parent, "HTML", self._export_html, chrome=chrome
        )
        self.btn_export_diff.pack(side=tk.LEFT, **gap)
        self.btn_export_html.pack(side=tk.LEFT, **gap)
        self._chrome_btn(
            parent, "‹", lambda: self._goto_change(-1), chrome=chrome, tip="上一处 Shift+F7"
        ).pack(side=tk.LEFT, **gap)
        self._chrome_btn(
            parent, "›", lambda: self._goto_change(1), chrome=chrome, tip="下一处 F7"
        ).pack(side=tk.LEFT, **gap)

        self._busy_widgets = [
            self.btn_compare,
            self.btn_export_diff,
            self.btn_export_html,
        ]
        self._tip_a = _Tooltip(self.btn_a, "")
        self._tip_b = _Tooltip(self.btn_b, "")
        self.var_a.trace_add("write", lambda *_: self._refresh_dir_tips())
        self.var_b.trace_add("write", lambda *_: self._refresh_dir_tips())

    def _chrome_btn(
        self,
        parent: tk.Misc,
        text: str,
        command,
        *,
        chrome: bool,
        accent: bool = False,
        tip: str = "",
    ) -> tk.Button:
        if chrome:
            bg = ACCENT if accent else TITLE_BG
            abg = "#1a7aeb" if accent else HOVER
            fg = "#ffffff" if accent else INK
            btn = tk.Button(
                parent,
                text=text,
                command=command,
                bg=bg,
                fg=fg,
                activebackground=abg,
                activeforeground="#ffffff",
                disabledforeground=MUTED,
                relief=tk.FLAT,
                bd=0,
                padx=7,
                pady=2,
                font=self._ui,
                cursor="hand2",
                highlightthickness=0,
            )
        else:
            style = "Accent.TButton" if accent else "TButton"
            # Keep ttk for non-chrome; wrap for busy list compatibility via state()
            btn = ttk.Button(parent, text=text, command=command, style=style)  # type: ignore[assignment]
        if tip:
            _Tooltip(btn, tip)
        return btn  # type: ignore[return-value]

    def _build_top_toolbar(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(fill=tk.X, padx=8, pady=(6, 4))
        self._build_action_cluster(bar, chrome=False)
        ttk.Button(bar, text="侧栏", width=4, command=self._toggle_sidebar).pack(
            side=tk.RIGHT
        )

    def _build_footer(self) -> None:
        foot = ttk.Frame(self, style="Footer.TFrame")
        foot.pack(fill=tk.X, side=tk.BOTTOM, padx=0, pady=0)

        inner = ttk.Frame(foot, style="Footer.TFrame")
        inner.pack(fill=tk.X, padx=8, pady=4)

        btn_ws = ttk.Checkbutton(
            inner,
            text="忽略空白",
            variable=self.var_ignore_ws,
            command=self._on_ignore_ws_toggle,
            style="Footer.TCheckbutton",
        )
        btn_ws.pack(side=tk.LEFT)
        _Tooltip(btn_ws, "忽略行内多余空白，并忽略空行增删")
        btn_comments = ttk.Checkbutton(
            inner,
            text="忽略注释",
            variable=self.var_ignore_comments,
            command=self._on_ignore_comments_toggle,
            style="Footer.TCheckbutton",
        )
        btn_comments.pack(side=tk.LEFT, padx=(8, 0))
        _Tooltip(btn_comments, "忽略仅改注释内容的差异；新增/删除注释行仍显示")
        ttk.Checkbutton(
            inner,
            text="自动换行",
            variable=self.var_wrap,
            command=self._on_wrap_toggle,
            style="Footer.TCheckbutton",
        ).pack(side=tk.LEFT, padx=(8, 0))
        btn_enc = ttk.Checkbutton(
            inner,
            text="仅编码",
            variable=self.var_show_encoding_only,
            command=self._on_show_encoding_only_toggle,
            style="Footer.TCheckbutton",
        )
        btn_enc.pack(side=tk.LEFT, padx=(8, 0))
        _Tooltip(
            btn_enc,
            "默认隐藏「仅编码」（Unicode 相同，多为 EOL/BOM）。勾选后显示在列表中。",
        )

        ttk.Separator(inner, orient=tk.VERTICAL).pack(
            side=tk.LEFT, fill=tk.Y, padx=10, pady=2
        )

        self.meta_lang = ttk.Label(inner, text="语言: —", style="Footer.TLabel")
        self.meta_lang.pack(side=tk.LEFT)
        self.meta_enc = ttk.Label(inner, text="编码: —", style="Footer.TLabel")
        self.meta_enc.pack(side=tk.LEFT, padx=(12, 0))

        self.progress = ttk.Progressbar(
            inner,
            style="Footer.Horizontal.TProgressbar",
            mode="determinate",
            length=160,
            maximum=100,
            value=0,
        )
        self.status = ttk.Label(
            inner,
            text="点「旧侧 / 新侧」·「编码检查」可对当前两侧统一为 UTF-8 无 BOM+LF",
            style="Status.TLabel",
        )
        self.status.pack(side=tk.RIGHT, padx=(10, 0))
        # Progress sits at far right when shown (packed after status with side=RIGHT).

    def _build_title_bar(self) -> None:
        bar = tk.Frame(self, bg=TITLE_BG, height=38)
        bar.pack(fill=tk.X, side=tk.TOP)
        bar.pack_propagate(False)

        left = tk.Frame(bar, bg=TITLE_BG)
        left.pack(side=tk.LEFT, padx=(10, 0))
        tk.Label(
            left,
            text="▣",
            bg=TITLE_BG,
            fg=ACCENT,
            font=(self._ui[0], 11),
        ).pack(side=tk.LEFT)
        tk.Label(
            left,
            text="目录对比",
            bg=TITLE_BG,
            fg=INK,
            font=self._ui,
        ).pack(side=tk.LEFT, padx=(6, 8))

        actions = tk.Frame(bar, bg=TITLE_BG)
        actions.pack(side=tk.LEFT)
        self._build_action_cluster(actions, chrome=True)

        drag = tk.Frame(bar, bg=TITLE_BG)
        drag.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        for w in (left, drag):
            self._bind_title_drag(w)

        btns = tk.Frame(bar, bg=TITLE_BG)
        btns.pack(side=tk.RIGHT)
        self._title_btn(btns, "☰", self._toggle_sidebar, tip="显隐侧栏")
        self._title_btn(btns, "—", self._minimize_window)
        self._btn_max = self._title_btn(btns, "□", self._toggle_maximize)
        self._title_btn(btns, "✕", self._on_close, close=True)

    def _bind_title_drag(self, widget: tk.Widget) -> None:
        widget.bind("<ButtonPress-1>", self._title_press)
        widget.bind("<B1-Motion>", self._title_drag)
        widget.bind("<Double-Button-1>", lambda e: self._toggle_maximize())
        for child in widget.winfo_children():
            self._bind_title_drag(child)

    def _title_btn(
        self,
        parent: tk.Frame,
        text: str,
        command,
        *,
        close: bool = False,
        tip: str = "",
    ) -> tk.Label:
        lbl = tk.Label(
            parent,
            text=text,
            bg=TITLE_BG,
            fg=INK,
            width=4,
            font=(self._ui[0], 10),
            cursor="hand2",
        )
        lbl.pack(side=tk.LEFT, fill=tk.Y)

        def enter(_event=None) -> None:
            if close:
                lbl.configure(bg=CLOSE_HOVER, fg="#ffffff")
            else:
                lbl.configure(bg=HOVER, fg="#ffffff")

        def leave(_event=None) -> None:
            lbl.configure(bg=TITLE_BG, fg=INK)

        lbl.bind("<Enter>", enter)
        lbl.bind("<Leave>", leave)
        lbl.bind("<Button-1>", lambda e: command())
        if tip:
            _Tooltip(lbl, tip)
        return lbl

    def _title_press(self, event) -> None:
        if self._maximized:
            self._drag_offset = None
            return
        self._drag_offset = (event.x_root - self.winfo_x(), event.y_root - self.winfo_y())

    def _title_drag(self, event) -> None:
        if self._drag_offset is None or self._maximized:
            return
        x = event.x_root - self._drag_offset[0]
        y = event.y_root - self._drag_offset[1]
        self.geometry(f"+{x}+{y}")

    def _work_area(self) -> tuple[int, int, int, int]:
        if sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes

                class RECT(ctypes.Structure):
                    _fields_ = [
                        ("left", wintypes.LONG),
                        ("top", wintypes.LONG),
                        ("right", wintypes.LONG),
                        ("bottom", wintypes.LONG),
                    ]

                rect = RECT()
                SPI_GETWORKAREA = 48
                if ctypes.windll.user32.SystemParametersInfoW(
                    SPI_GETWORKAREA, 0, ctypes.byref(rect), 0
                ):
                    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
            except Exception:
                pass
        return 0, 0, self.winfo_screenwidth(), self.winfo_screenheight()

    def _toggle_maximize(self) -> None:
        if self._maximized:
            self.geometry(self._restore_geometry)
            self._maximized = False
            if hasattr(self, "_btn_max"):
                self._btn_max.configure(text="□")
        else:
            self._restore_geometry = self.geometry()
            x, y, w, h = self._work_area()
            self.geometry(f"{w}x{h}+{x}+{y}")
            self._maximized = True
            if hasattr(self, "_btn_max"):
                self._btn_max.configure(text="❐")

    def _minimize_window(self) -> None:
        if not self._custom_chrome:
            self.iconify()
            return
        # Temporarily restore native frame so Windows shows a taskbar button.
        self._taskbar_done = False
        self.overrideredirect(False)
        self.update_idletasks()
        self.iconify()

    def _on_map_restore_chrome(self, _event=None) -> None:
        if not self._custom_chrome or self._taskbar_refreshing:
            return
        try:
            if self.state() == "normal":
                self.overrideredirect(True)
                self.after(30, self._ensure_taskbar_icon)
        except tk.TclError:
            pass

    def _ensure_taskbar_icon(self) -> None:
        """overrideredirect hides the taskbar entry; force WS_EX_APPWINDOW."""
        if not self._custom_chrome or sys.platform != "win32":
            return
        if self._taskbar_refreshing:
            return
        try:
            import ctypes

            self.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            if not hwnd:
                hwnd = int(self.winfo_id())
            GWL_EXSTYLE = -20
            WS_EX_APPWINDOW = 0x00040000
            WS_EX_TOOLWINDOW = 0x00000080
            get_long = ctypes.windll.user32.GetWindowLongW
            set_long = ctypes.windll.user32.SetWindowLongW
            if ctypes.sizeof(ctypes.c_void_p) == 8 and hasattr(
                ctypes.windll.user32, "GetWindowLongPtrW"
            ):
                get_long = ctypes.windll.user32.GetWindowLongPtrW
                set_long = ctypes.windll.user32.SetWindowLongPtrW
            style = get_long(hwnd, GWL_EXSTYLE)
            new_style = (style & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW
            set_long(hwnd, GWL_EXSTYLE, new_style)
            SWP_NOSIZE = 0x0001
            SWP_NOMOVE = 0x0002
            SWP_NOZORDER = 0x0004
            SWP_FRAMECHANGED = 0x0020
            ctypes.windll.user32.SetWindowPos(
                hwnd,
                0,
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED,
            )
            if getattr(self, "_taskbar_done", False):
                return
            self._taskbar_done = True
            self._taskbar_refreshing = True
            self.wm_withdraw()
            self.after(20, self._finish_taskbar_refresh)
        except Exception:
            self._taskbar_refreshing = False

    def _finish_taskbar_refresh(self) -> None:
        try:
            self.wm_deiconify()
            if self._custom_chrome:
                self.overrideredirect(True)
        finally:
            self._taskbar_refreshing = False
    def _toggle_sidebar(self) -> None:
        self._set_sidebar_visible(not self._sidebar_visible)

    def _set_sidebar_visible(self, visible: bool, *, persist: bool = True) -> None:
        self._sidebar_visible = visible
        for pane in list(self._body.panes()):
            self._body.forget(pane)
        if visible:
            self._body.add(self._left_pane, weight=1)
        self._body.add(self._right_pane, weight=4)
        if persist:
            self._persist_prefs()

    def _center_toplevel(
        self,
        win: tk.Toplevel,
        width: int | None = None,
        height: int | None = None,
    ) -> None:
        self.update_idletasks()
        win.update_idletasks()
        w = width or max(win.winfo_reqwidth(), win.winfo_width(), 200)
        h = height or max(win.winfo_reqheight(), win.winfo_height(), 120)
        px = self.winfo_rootx()
        py = self.winfo_rooty()
        pw = max(self.winfo_width(), 1)
        ph = max(self.winfo_height(), 1)
        x = px + (pw - w) // 2
        y = py + (ph - h) // 2
        win.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")

    def _ask_directory(
        self,
        *,
        parent: tk.Misc | None = None,
        initialdir: str | None = None,
        title: str | None = None,
    ) -> str:
        """Native folder dialog, centered relative to parent via an invisible anchor."""
        owner = parent or self
        owner.update_idletasks()
        anchor = tk.Toplevel(owner)
        anchor.withdraw()
        cx = owner.winfo_rootx() + max(owner.winfo_width(), 1) // 2
        cy = owner.winfo_rooty() + max(owner.winfo_height(), 1) // 2
        anchor.geometry(f"1x1+{cx}+{cy}")
        anchor.transient(owner)
        anchor.overrideredirect(True)
        try:
            anchor.attributes("-alpha", 0.0)
        except tk.TclError:
            pass
        anchor.deiconify()
        anchor.update_idletasks()
        kwargs: dict = {"parent": anchor}
        if initialdir:
            kwargs["initialdir"] = initialdir
        if title:
            kwargs["title"] = title
        try:
            path = filedialog.askdirectory(**kwargs)
        finally:
            try:
                anchor.destroy()
            except tk.TclError:
                pass
        return path or ""

    def _encoding_prepare(self) -> None:
        """Embedded encoding_unify: check (and optionally convert) left/right trees."""
        if self._busy:
            messagebox.showinfo("提示", "正在处理中，请稍后再试")
            return
        a = self.var_a.get().strip()
        b = self.var_b.get().strip()
        if not a or not b:
            messagebox.showwarning("提示", "请先选择旧侧 / 新侧目录")
            return
        left, right = Path(a), Path(b)
        if not left.is_dir() or not right.is_dir():
            messagebox.showwarning("提示", "旧侧或新侧不是有效目录")
            return

        try:
            from encoding_bridge import prepare_for_diff, unify_available
        except Exception as exc:
            messagebox.showerror("错误", f"无法加载编码桥接：{exc}")
            return
        if not unify_available():
            messagebox.showwarning(
                "提示",
                "未找到 E:\\tools\\encoding_unify\n请安装/放置转码库后再用「编码检查」。",
            )
            return

        self._set_busy(True, "正在检查左右目录编码…")

        def work() -> None:
            err: str | None = None
            summary = None
            try:
                summary = prepare_for_diff(left, right, apply=False)
            except Exception as exc:
                err = str(exc)

            def done() -> None:
                self._set_busy(False)
                if err or summary is None:
                    messagebox.showerror("编码检查失败", err or "未知错误")
                    self.status.config(text="编码检查失败")
                    return
                self.status.config(
                    text=(
                        f"编码检查: 待转码 "
                        f"{summary.left_to_convert + summary.right_to_convert} · "
                        f"待换行/BOM "
                        f"{summary.left_eol_or_bom + summary.right_eol_or_bom} · "
                        f"待复核 {summary.left_review + summary.right_review}"
                    )
                )
                self._show_encoding_prep_dialog(summary, left, right)

            self.after(0, done)

        threading.Thread(target=work, daemon=True).start()

    def _show_encoding_prep_dialog(self, summary, left: Path, right: Path) -> None:
        win = tk.Toplevel(self)
        win.title("编码检查（内嵌 encoding_unify）")
        win.configure(bg=BG)
        win.transient(self)
        win.minsize(520, 360)
        self._center_toplevel(win, 560, 420)

        ttk.Label(
            win,
            text="Diff 只读对比；统一写盘由 encoding_unify（UTF-8 无 BOM + LF，带 _encoding_backup）",
            style="Muted.TLabel",
        ).pack(anchor=tk.W, padx=12, pady=(12, 6))

        text = tk.Text(
            win,
            wrap=tk.WORD,
            font=self._mono,
            bg=DIFF_BG,
            fg=INK,
            insertbackground=INK,
            relief=tk.FLAT,
            padx=8,
            pady=6,
            highlightthickness=1,
            highlightbackground=LINE,
            height=16,
        )
        text.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 8))
        text.insert("1.0", summary.text())
        text.configure(state=tk.DISABLED)

        btns = ttk.Frame(win)
        btns.pack(fill=tk.X, padx=12, pady=(0, 12))

        def close() -> None:
            win.destroy()

        def run_compare() -> None:
            win.destroy()
            self._start_compare()

        def convert_then_compare() -> None:
            if not messagebox.askyesno(
                "确认统一编码与换行",
                "将把旧侧/新侧文本文件统一为：\n"
                "  · UTF-8 无 BOM\n"
                "  · 换行 LF（\\n）\n\n"
                "会在各自目录下生成 _encoding_backup。\n"
                "请确认目录是副本或已备份。是否继续？",
                parent=win,
            ):
                return
            win.destroy()
            self._set_busy(True, "正在统一为 UTF-8 无 BOM + LF…")

            def work() -> None:
                err: str | None = None
                summary2 = None
                try:
                    from encoding_bridge import prepare_for_diff

                    summary2 = prepare_for_diff(left, right, apply=True)
                except Exception as exc:
                    err = str(exc)

                def done() -> None:
                    self._set_busy(False)
                    if err or summary2 is None:
                        messagebox.showerror("统一失败", err or "未知错误")
                        return
                    messagebox.showinfo("统一完成", summary2.text(), parent=self)
                    self.status.config(text="已统一 UTF-8+LF · 开始对比")
                    self._start_compare()

                self.after(0, done)

            threading.Thread(target=work, daemon=True).start()

        ttk.Button(btns, text="关闭", command=close).pack(side=tk.RIGHT)
        ttk.Button(btns, text="直接运行 Diff", command=run_compare).pack(
            side=tk.RIGHT, padx=(0, 8)
        )
        if not summary.ok_for_diff:
            ttk.Button(
                btns, text="统一 UTF-8+LF 后运行", style="Accent.TButton", command=convert_then_compare
            ).pack(side=tk.RIGHT, padx=(0, 8))
        else:
            ttk.Button(
                btns, text="运行 Diff", style="Accent.TButton", command=run_compare
            ).pack(side=tk.RIGHT, padx=(0, 8))

    def _edit_ignore_patterns(self) -> None:
        a = self.var_a.get().strip()
        b = self.var_b.get().strip()
        roots = [Path(p) for p in (a, b) if p]
        if not roots:
            messagebox.showinfo("提示", "请先选择旧侧 / 新侧目录，以便选择文件夹与识别类型")
            return

        win = tk.Toplevel(self)
        win.title("规则")
        win.configure(bg=BG)
        win.transient(self)
        win.minsize(600, 360)
        self._center_toplevel(win, 760, 480)

        baseline = [dict(r) for r in self._ignore_rules]
        dirty = {"changed": False}
        cards: list[dict] = []

        top = ttk.Frame(win)
        top.pack(fill=tk.X, padx=12, pady=(12, 6))
        ttk.Label(
            top,
            text="文件夹可手填或点「目录」选择 · 类型随文件夹变化 · 关闭时若有修改将自动运行",
            style="Muted.TLabel",
        ).pack(side=tk.LEFT)
        ttk.Button(top, text="新增规则", command=lambda: add_card()).pack(side=tk.RIGHT)

        canvas_wrap = ttk.Frame(win)
        canvas_wrap.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))
        canvas = tk.Canvas(canvas_wrap, bg=BG, highlightthickness=0, borderwidth=0)
        vbar = ttk.Scrollbar(canvas_wrap, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=vbar.set)
        vbar.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        inner = ttk.Frame(canvas)
        inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _sync_scroll(_event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure(inner_id, width=canvas.winfo_width())

        inner.bind("<Configure>", _sync_scroll)
        canvas.bind("<Configure>", _sync_scroll)

        def _on_mousewheel(event) -> str:
            if hasattr(event, "delta") and event.delta:
                delta = -1 if event.delta > 0 else 1
            else:
                delta = -1 if getattr(event, "num", 0) == 4 else 1
            canvas.yview_scroll(delta, "units")
            return "break"

        def _bind_wheel(widget: tk.Misc) -> None:
            widget.bind("<MouseWheel>", _on_mousewheel, add="+")
            widget.bind("<Button-4>", _on_mousewheel, add="+")
            widget.bind("<Button-5>", _on_mousewheel, add="+")

        _bind_wheel(canvas)
        _bind_wheel(inner)

        def folder_display(folder: str) -> str:
            return _normalize_folder(folder)

        def types_display(types: str) -> tuple[str, bool]:
            raw = types.strip()
            neg = raw.startswith("!")
            body = raw[1:].strip() if neg else raw
            if not body:
                return TYPE_ALL, neg
            first = re.split(r"[,;\s]+", body)[0]
            neg2, spec = _normalize_type_token(("!" if neg else "") + first)
            return (spec or TYPE_ALL), neg2

        def folder_value(display: str) -> str:
            text = display.strip()
            if not text or text == FOLDER_ANY:
                return ""
            return _normalize_folder(text)

        def types_value(display: str, negate: bool) -> str:
            text = display.strip()
            if not text or text == TYPE_ALL:
                return ""
            _, spec = _normalize_type_token(text)
            if not spec:
                return ""
            return ("!" + spec) if negate else spec

        def type_choices_for(folder_text: str) -> list[str]:
            folder = folder_value(folder_text)
            exts = scan_exts_under(folder, roots)
            return [TYPE_ALL, *exts]

        def commit() -> None:
            rules: list[dict[str, str]] = []
            for card in cards:
                folder = folder_value(card["folder_var"].get())
                types = types_value(card["type_var"].get(), bool(card["neg_var"].get()))
                if not folder and not types:
                    continue
                rules.append({"folder": folder, "types": types})
            dirty["changed"] = rules != baseline
            self._ignore_rules = rules
            self._ignore_patterns = compile_ignore_rules(rules)
            self._persist_prefs()
            if dirty["changed"]:
                self.status.config(
                    text=f"忽略规则已更新（{len(rules)} 条，关闭窗口后自动运行）"
                )
            else:
                self.status.config(text=f"忽略规则（{len(rules)} 条）")

        def remove_card(card: dict) -> None:
            card["frame"].destroy()
            cards.remove(card)
            commit()
            _sync_scroll()

        def refresh_types(card: dict, *, keep_current: bool = True) -> None:
            choices = type_choices_for(card["folder_var"].get())
            current = card["type_var"].get().strip() or TYPE_ALL
            if keep_current and current not in choices and current != TYPE_ALL:
                choices = [TYPE_ALL, current, *[c for c in choices if c != TYPE_ALL]]
            elif current not in choices:
                card["type_var"].set(TYPE_ALL)
            card["type_combo"].set_values(choices)

        def add_card(rule: dict[str, str] | None = None) -> None:
            rule = rule or {"folder": "", "types": ""}
            frame = tk.Frame(
                inner,
                bg=PANEL,
                highlightbackground=LINE,
                highlightthickness=1,
                padx=10,
                pady=8,
            )
            frame.pack(fill=tk.X, pady=(0, 8))

            row = tk.Frame(frame, bg=PANEL)
            row.pack(fill=tk.X)

            tk.Label(row, text="此条规则忽略", bg=PANEL, fg=INK, font=self._ui).pack(
                side=tk.LEFT
            )

            folder_var = tk.StringVar(value=folder_display(rule.get("folder", "")))
            type_disp, neg = types_display(rule.get("types", ""))
            type_var = tk.StringVar(value=type_disp)
            neg_var = tk.BooleanVar(value=neg)

            folder_box = ttk.Frame(row)
            folder_box.pack(side=tk.LEFT, padx=6)
            folder_entry = ttk.Entry(folder_box, textvariable=folder_var, width=22)
            folder_entry.pack(side=tk.LEFT)
            ttk.Button(folder_box, text="目录", width=4).pack(side=tk.LEFT, padx=(4, 0))

            tk.Label(row, text="文件夹下的", bg=PANEL, fg=INK, font=self._ui).pack(
                side=tk.LEFT
            )

            t_vals = type_choices_for(folder_var.get())
            if type_var.get() and type_var.get() not in t_vals:
                t_vals = [TYPE_ALL, type_var.get(), *[c for c in t_vals if c != TYPE_ALL]]

            type_combo = SearchCombo(
                row, t_vals, textvariable=type_var, width=14, on_change=commit
            )
            type_combo.pack(side=tk.LEFT, padx=6)
            tk.Label(row, text="类型文件", bg=PANEL, fg=INK, font=self._ui).pack(
                side=tk.LEFT
            )

            neg_chk = ttk.Checkbutton(
                row, text="反格式", variable=neg_var, command=commit
            )
            neg_chk.pack(side=tk.LEFT, padx=(10, 0))

            card: dict = {
                "frame": frame,
                "folder_var": folder_var,
                "type_var": type_var,
                "neg_var": neg_var,
                "type_combo": type_combo,
            }

            def browse_folder() -> None:
                initial = roots[0]
                current = folder_value(folder_var.get())
                if current:
                    for root in roots:
                        candidate = root / current
                        if candidate.is_dir():
                            initial = candidate
                            break
                path = self._ask_directory(
                    parent=win,
                    initialdir=str(initial),
                    title="选择要忽略的文件夹（请在对比目录内）",
                )
                if not path:
                    return
                rel = rel_path_under_roots(Path(path), roots)
                if rel is None:
                    messagebox.showwarning(
                        "提示",
                        "请选择旧侧或新侧目录下的文件夹",
                        parent=win,
                    )
                    return
                folder_var.set(rel)
                refresh_types(card)
                commit()

            for child in folder_box.winfo_children():
                if isinstance(child, ttk.Button):
                    child.configure(command=browse_folder)

            def on_folder_edited(_event=None) -> None:
                refresh_types(card)
                commit()

            folder_entry.bind("<FocusOut>", on_folder_edited, add="+")
            folder_entry.bind("<Return>", on_folder_edited, add="+")

            tk.Button(
                row,
                text="删除",
                command=lambda c=card: remove_card(c),
                bg=LINE,
                fg=INK,
                activebackground=CLOSE_HOVER,
                activeforeground="#ffffff",
                relief=tk.FLAT,
                bd=0,
                padx=8,
                pady=2,
                font=self._ui,
                cursor="hand2",
                highlightthickness=0,
            ).pack(side=tk.RIGHT)

            cards.append(card)
            _bind_wheel(frame)
            for child in frame.winfo_children():
                _bind_wheel(child)
            _sync_scroll()
            canvas.yview_moveto(1.0)

        def on_close() -> None:
            commit()
            changed = dirty["changed"]
            win.destroy()
            if changed and self.var_a.get().strip() and self.var_b.get().strip():
                self.after(80, self._start_compare)

        win.protocol("WM_DELETE_WINDOW", on_close)

        if baseline:
            for rule in baseline:
                add_card(rule)
        else:
            add_card()
        # Re-center after content laid out (req size may grow).
        self.after_idle(lambda: self._center_toplevel(win, 760, 480))

    def _update_file_meta(self, change: FileChange | None) -> None:
        if change is None:
            self.meta_lang.config(text="语言: —")
            self.meta_enc.config(text="编码: —")
            return
        sample_lines = change.lines_a or change.lines_b or []
        sample = "\n".join(sample_lines[:40])
        lang = detect_language(change.rel, sample)
        self.meta_lang.config(text=f"语言: {lang}")

        ea = change.encoding_a or "—"
        eb = change.encoding_b or "—"
        if change.encoding_a and change.encoding_b:
            if change.encoding_a == change.encoding_b:
                text = f"编码: {ea}"
            else:
                text = f"编码: 旧 {ea} / 新 {eb} ⚠"
            if change.encoding_only:
                text += " · 仅编码"
            self.meta_enc.config(text=text)
            return

        # Fallback: probe from path if compare did not record encoding.
        def enc_of(path: str | None) -> str:
            if not path:
                return "—"
            try:
                raw = Path(path).read_bytes()
                if len(raw) > 262144:
                    raw = raw[:262144]
                return detect_and_decode(raw).encoding
            except OSError:
                return "?"

        if change.encoding_a or change.encoding_b:
            self.meta_enc.config(text=f"编码: {ea if change.encoding_a else enc_of(change.path_a)}")
        else:
            ea2, eb2 = enc_of(change.path_a), enc_of(change.path_b)
            if ea2 == eb2:
                self.meta_enc.config(text=f"编码: {ea2}")
            else:
                self.meta_enc.config(text=f"编码: 旧 {ea2} / 新 {eb2} ⚠")

    def _make_diff_text(self, parent: ttk.Frame) -> tk.Text:
        text = tk.Text(
            parent,
            wrap=tk.NONE,
            font=self._mono,
            bg=DIFF_BG,
            fg=INK,
            insertbackground=INK,
            relief=tk.FLAT,
            padx=8,
            pady=6,
            highlightthickness=1,
            highlightbackground=LINE,
            highlightcolor=ACCENT,
            state=tk.DISABLED,
            cursor="arrow",
        )
        text.tag_configure("equal", foreground="#d8d8d8")
        # GitHub-like: red remove / green add. No bg on equal/empty — avoids a
        # gray strip bleeding onto the first line of each change hunk in Tk.
        text.tag_configure("delete", foreground="#ffa198", background="#3a1212")
        text.tag_configure("insert", foreground="#7ee787", background="#0d2818")
        text.tag_configure("replace", foreground="#d8d8d8")
        text.tag_configure("empty", foreground="#3a3a3a")
        text.tag_configure("inline_del", background="#5c1818", foreground="#ffd7d5")
        text.tag_configure("inline_add", background="#144028", foreground="#dcffe4")
        # Current hunk: scroll only — no third accent color on gutters.
        text.tag_configure("meta", foreground=MUTED)
        text.tag_configure("ln", foreground="#5a5a5a")
        text.tag_configure("ln_del", foreground="#ffa198", background="#3a1212")
        text.tag_configure("ln_add", foreground="#7ee787", background="#0d2818")
        for name in (
            "delete",
            "insert",
            "ln_del",
            "ln_add",
            "inline_del",
            "inline_add",
        ):
            text.tag_raise(name)
        return text

    def _refresh_dir_tips(self) -> None:
        a = self.var_a.get().strip() or "未选择旧侧目录"
        b = self.var_b.get().strip() or "未选择新侧目录"
        self._tip_a.text = a
        self._tip_b.text = b

    def _browse(self, var: tk.StringVar) -> None:
        initial = var.get().strip() or None
        path = self._ask_directory(initialdir=initial, title="选择目录")
        if path:
            var.set(path)
            self._persist_prefs()
            self._refresh_dir_tips()
            side = "旧侧" if var is self.var_a else "新侧"
            self.status.config(text=f"{side}: {path}")

    def _swap_dirs(self) -> None:
        a, b = self.var_a.get(), self.var_b.get()
        self.var_a.set(b)
        self.var_b.set(a)
        self._persist_prefs()
        if a.strip() and b.strip():
            self._start_compare()

    def _on_ignore_ws_toggle(self) -> None:
        self._render_cache.clear()
        self._persist_prefs()
        if self.var_a.get().strip() and self.var_b.get().strip():
            self._start_compare()

    def _on_ignore_comments_toggle(self) -> None:
        self._render_cache.clear()
        self._persist_prefs()
        if self.var_a.get().strip() and self.var_b.get().strip():
            self._start_compare()

    def _on_wrap_toggle(self) -> None:
        self._render_cache.clear()
        self._apply_wrap()
        self._persist_prefs()
        if self._current_change is not None:
            self._show_change(self._current_change)

    def _on_show_encoding_only_toggle(self) -> None:
        self._persist_prefs()
        self._apply_filter()

    def _on_diff_configure(self, _event=None) -> None:
        if not self.var_wrap.get() or self._current_change is None or self._busy:
            return
        size = (self.left_text.winfo_width(), self.right_text.winfo_width())
        if size == self._last_diff_size or size[0] < 40 or size[1] < 40:
            return
        self._last_diff_size = size
        if self._wrap_resize_after is not None:
            try:
                self.after_cancel(self._wrap_resize_after)
            except tk.TclError:
                pass
        self._wrap_resize_after = self.after(250, self._rerender_for_wrap)

    def _rerender_for_wrap(self) -> None:
        self._wrap_resize_after = None
        self._render_cache.clear()
        if self._current_change is not None and not self._busy:
            self._show_change(self._current_change)

    def _apply_wrap(self) -> None:
        mode = tk.CHAR if self.var_wrap.get() else tk.NONE
        for text in (self.left_text, self.right_text):
            text.configure(wrap=mode)

    def _persist_prefs(self) -> None:
        geo = self._restore_geometry if self._maximized else self.geometry()
        save_settings(
            {
                "dir_a": self.var_a.get().strip(),
                "dir_b": self.var_b.get().strip(),
                "ignore_whitespace": bool(self.var_ignore_ws.get()),
                "ignore_comments": bool(self.var_ignore_comments.get()),
                "word_wrap": bool(self.var_wrap.get()),
                "show_encoding_only": bool(self.var_show_encoding_only.get()),
                "geometry": geo,
                "ignore_patterns": list(self._ignore_patterns),
                "ignore_rules": [dict(r) for r in self._ignore_rules],
                "sidebar_visible": bool(self._sidebar_visible),
            }
        )

    def _set_busy(self, busy: bool, status: str | None = None) -> None:
        self._busy = busy
        for btn in self._busy_widgets:
            try:
                if isinstance(btn, ttk.Button):
                    btn.state(["disabled"] if busy else ["!disabled"])
                else:
                    btn.configure(state=tk.DISABLED if busy else tk.NORMAL)
            except tk.TclError:
                pass
        if status is not None:
            self.status.config(text=status)
        if not busy:
            self._hide_progress()

    def _show_progress(self, current: int, total: int) -> None:
        if total <= 0:
            self._hide_progress()
            return
        try:
            self.progress.configure(maximum=max(total, 1), value=max(0, min(current, total)))
            if not self.progress.winfo_ismapped():
                self.progress.pack(side=tk.RIGHT, before=self.status, padx=(8, 0))
        except tk.TclError:
            pass

    def _hide_progress(self) -> None:
        try:
            self.progress.configure(value=0)
            if self.progress.winfo_ismapped():
                self.progress.pack_forget()
        except tk.TclError:
            pass

    def _compare_fingerprint(self) -> dict:
        return cache_fingerprint(
            self.var_a.get().strip(),
            self.var_b.get().strip(),
            ignore_whitespace=bool(self.var_ignore_ws.get()),
            ignore_comments=bool(self.var_ignore_comments.get()),
            ignore_patterns=list(self._ignore_patterns),
        )

    def _try_restore_or_compare(self) -> None:
        """Open last cached result if fingerprint matches; otherwise scan."""
        cached = load_compare_cache(self._compare_fingerprint())
        if cached is None:
            self._start_compare()
            return
        result, selected = cached
        self.status.config(text="正在从缓存恢复…")
        self._apply_result(result, preserve_rel=selected or None, from_cache=True)

    def _on_compare(self) -> None:
        self._start_compare()

    def _start_compare(self, *, preserve_rel: str | None = None) -> None:
        if self._busy:
            messagebox.showinfo("提示", "正在处理中，请稍后再试")
            return
        a = self.var_a.get().strip()
        b = self.var_b.get().strip()
        if not a or not b:
            messagebox.showwarning("提示", "请先选择两个目录")
            return
        if preserve_rel is None:
            preserve_rel = self._selected_rel()
        self._set_busy(True, "正在扫描对比…")
        self._show_progress(0, 1)
        ignore_ws = bool(self.var_ignore_ws.get())
        ignore_comments = bool(self.var_ignore_comments.get())
        extra = list(self._ignore_patterns)
        threading.Thread(
            target=self._run_compare,
            args=(Path(a), Path(b), preserve_rel, ignore_ws, ignore_comments, extra),
            daemon=True,
        ).start()

    def _run_compare(
        self,
        dir_a: Path,
        dir_b: Path,
        preserve_rel: str | None,
        ignore_ws: bool,
        ignore_comments: bool,
        extra_ignore: list[str],
    ) -> None:
        import time

        last_ui = [0.0]

        def on_progress(current: int, total: int, rel: str) -> None:
            now = time.monotonic()
            if current < total and now - last_ui[0] < 0.05:
                return
            last_ui[0] = now
            short = rel if len(rel) <= 48 else "…" + rel[-47:]
            pct = int(100 * current / total) if total else 0
            text = f"扫描 {current}/{total}（{pct}%）：{short}"

            def tick(c=current, t=total, msg=text) -> None:
                self._show_progress(c, t)
                self.status.config(text=msg)

            self.after(0, tick)

        try:
            result = compute_diff(
                dir_a,
                dir_b,
                ignore_whitespace=ignore_ws,
                ignore_comments=ignore_comments,
                extra_ignore=extra_ignore,
                progress=on_progress,
            )
        except Exception as exc:
            result = CompareResult(error=str(exc))
        self.after(0, self._apply_result, result, preserve_rel)

    def _apply_result(
        self,
        result: CompareResult,
        preserve_rel: str | None = None,
        *,
        from_cache: bool = False,
    ) -> None:
        self._set_busy(False)
        self._clear_diff()
        self._all_changes = []
        self._filtered = []
        self._last_result = None
        self._current_change = None
        self._update_file_meta(None)

        if result.error:
            self.status.config(text=f"错误: {result.error}")
            messagebox.showerror("错误", result.error)
            return

        self._last_result = result
        self._all_changes = result.changes
        self._align_cache.clear()
        self._render_cache.clear()
        self._persist_prefs()
        if not from_cache:
            save_compare_cache(
                self._compare_fingerprint(),
                result,
                selected_rel=preserve_rel,
            )

        self._apply_filter(preserve_rel=preserve_rel)

        ws = " · 忽略空白" if self.var_ignore_ws.get() else ""
        cm = " · 忽略注释" if self.var_ignore_comments.get() else ""
        ig = f" · 自定义忽略 {len(self._ignore_rules)}" if self._ignore_rules else ""
        enc_only = sum(1 for c in result.changes if c.encoding_only or c.kind == "encoding")
        enc_mis = sum(
            1
            for c in result.changes
            if c.encoding_a and c.encoding_b and c.encoding_a != c.encoding_b
        )
        eol_n = sum(
            1
            for c in result.changes
            if (c.encoding_only or c.kind == "encoding") and "eol" in (c.encoding_reason or "")
        )
        enc = ""
        if enc_mis:
            enc += f" · 编码不一致 {enc_mis}"
        if enc_only:
            if self.var_show_encoding_only.get():
                enc += f" · 仅编码 {enc_only}"
            else:
                enc += f" · 已隐藏仅编码 {enc_only}"
            if eol_n:
                enc += f"（EOL约{eol_n}）"
        cache_note = " · 缓存" if from_cache else ""
        if not result.changes:
            self.status.config(
                text=f"两个目录内容一致（在忽略规则下）{ws}{cm}{ig}{cache_note}"
            )
        else:
            shown = len(
                [
                    c
                    for c in result.changes
                    if self.var_show_encoding_only.get()
                    or not (c.encoding_only or c.kind == "encoding")
                ]
            )
            self.status.config(
                text=(
                    f"变更 {shown}/{len(result.changes)} 个文件 · "
                    f"仅 A={result.only_a} · 仅 B={result.only_b} · 共同={result.common}"
                    f"{ws}{cm}{ig}{enc}{cache_note}"
                )
            )
        if from_cache:
            tip = self.status.cget("text")
            if "点「运行」可刷新" not in tip:
                self.status.config(text=tip + " · 点「运行」可刷新")

    def _apply_filter(self, preserve_rel: str | None = None) -> None:
        if preserve_rel is None:
            preserve_rel = self._selected_rel()
        q = self.var_query.get().strip().lower()
        show_enc = bool(self.var_show_encoding_only.get())
        filtered: list[FileChange] = []
        for change in self._all_changes:
            if not show_enc and (change.encoding_only or change.kind == "encoding"):
                continue
            if q and q not in change.rel.lower() and q not in Path(change.rel).name.lower():
                continue
            filtered.append(change)
        self._filtered = filtered

        self.tree.delete(*self.tree.get_children())
        for i, change in enumerate(filtered):
            self.tree.insert(
                "",
                tk.END,
                iid=str(i),
                values=(Path(change.rel).name,),
            )

        if not filtered:
            self._current_change = None
            self._clear_diff()
            self._update_file_meta(None)
            if self._all_changes:
                self._set_diff_meta("没有符合过滤条件的文件。")
            elif self._last_result and not self._last_result.error:
                self._set_diff_meta("两个目录内容一致（在忽略规则下）。")
            return

        select_iid = "0"
        if preserve_rel:
            for i, change in enumerate(filtered):
                if change.rel == preserve_rel:
                    select_iid = str(i)
                    break
        self.tree.selection_set(select_iid)
        self.tree.focus(select_iid)
        self.tree.see(select_iid)
        self._show_change(filtered[int(select_iid)])

    def _on_tree_motion(self, event) -> None:
        row = self.tree.identify_row(event.y)
        if row == self._tree_tip_row:
            return
        self._tree_tip_row = row
        if not row:
            self._tree_tip.text = ""
            self._tree_tip._hide()
            return
        try:
            idx = int(row)
        except ValueError:
            return
        if 0 <= idx < len(self._filtered):
            self._tree_tip.text = self._filtered[idx].rel
        else:
            self._tree_tip.text = ""

    def _selected_rel(self) -> str | None:
        sel = self.tree.selection()
        if not sel:
            return None
        idx = int(sel[0])
        if 0 <= idx < len(self._filtered):
            return self._filtered[idx].rel
        return None

    def _on_select(self, _event=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        if 0 <= idx < len(self._filtered):
            change = self._filtered[idx]
            self._show_change(change)
            if self._last_result is not None:
                save_compare_cache(
                    self._compare_fingerprint(),
                    self._last_result,
                    selected_rel=change.rel,
                )

    def _on_tree_menu(self, event) -> None:
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
            self.tree.focus(row)
            self._on_select()
        try:
            self._menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._menu.grab_release()

    def _show_change(self, change: FileChange) -> None:
        self._current_change = change
        self._update_file_meta(change)
        self._show_gen += 1
        gen = self._show_gen
        if change.kind == "binary":
            self._clear_diff()
            self._set_diff_meta("二进制文件不同\n" + "\n".join(change.diff_lines))
            self._aligned = []
            self._change_rows = []
            self._change_cursor = -1
            return
        if change.kind == "encoding" or change.encoding_only:
            self._clear_diff()
            note = change.encoding_note or "\n".join(change.diff_lines)
            reason = change.encoding_reason or ""
            head = "仅编码不同（Unicode 文本相同，无 +/- hunk）"
            if reason:
                head += f"\n原因: {reason}"
            self._set_diff_meta(head + "\n" + note)
            self._aligned = []
            self._change_rows = []
            self._change_cursor = -1
            return

        ignore_ws = bool(self.var_ignore_ws.get())
        ignore_comments = bool(self.var_ignore_comments.get())
        rkey = self._render_cache_key(change)
        snap = self._render_cache.get(rkey)
        if snap is not None:
            self._render_cache.move_to_end(rkey)
            self._apply_snapshot(change, snap, gen)
            self._prefetch_neighbors(change)
            return

        cache_key = (change.rel, ignore_ws, ignore_comments)
        rows = self._align_cache.get(cache_key)
        if rows is not None:
            self._apply_aligned(change, rows, gen)
            self._prefetch_neighbors(change)
            return

        self.status.config(text=f"正在对齐: {change.rel} …")
        self._set_diff_meta(f"正在加载 {change.rel} …")
        self._aligned = []
        self._change_rows = []
        self._change_cursor = -1
        lines_a = list(change.lines_a)
        lines_b = list(change.lines_b)
        rel = change.rel

        def work() -> None:
            try:
                aligned = align_lines(
                    lines_a,
                    lines_b,
                    ignore_whitespace=ignore_ws,
                    ignore_comments=ignore_comments,
                    rel=rel,
                )
            except Exception as exc:
                self.after(0, self._align_failed, gen, rel, str(exc))
                return
            self.after(0, self._align_ready, gen, change, cache_key, aligned)

        threading.Thread(target=work, daemon=True).start()

    def _prefetch_neighbors(self, change: FileChange) -> None:
        """Warm align+render cache for adjacent list items (background)."""
        try:
            idx = self._filtered.index(change)
        except ValueError:
            return
        neighbors = []
        if idx > 0:
            neighbors.append(self._filtered[idx - 1])
        if idx + 1 < len(self._filtered):
            neighbors.append(self._filtered[idx + 1])
        ignore_ws = bool(self.var_ignore_ws.get())
        ignore_comments = bool(self.var_ignore_comments.get())
        wrap = bool(self.var_wrap.get())
        lw = max(self.left_text.winfo_width(), 80)
        rw = max(self.right_text.winfo_width(), 80)

        def warm(c: FileChange) -> None:
            if c.kind in {"binary", "encoding"} or c.encoding_only:
                return
            akey = (c.rel, ignore_ws, ignore_comments)
            rows = self._align_cache.get(akey)
            if rows is None:
                try:
                    rows = align_lines(
                        list(c.lines_a),
                        list(c.lines_b),
                        ignore_whitespace=ignore_ws,
                        ignore_comments=ignore_comments,
                        rel=c.rel,
                    )
                except Exception:
                    return
                self._align_cache[akey] = rows
            rkey = (c.rel, ignore_ws, ignore_comments, wrap, lw, rw)
            if rkey in self._render_cache:
                return
            snap = self._build_snapshot(
                rows,
                wrap=wrap,
                left_cols=self._cols_for_width(lw),
                right_cols=self._cols_for_width(rw),
            )

            def store(s=snap, k=rkey) -> None:
                self._store_render(k, s)

            self.after(0, store)

        for n in neighbors:
            if self._render_cache_key(n) in self._render_cache:
                continue
            threading.Thread(target=warm, args=(n,), daemon=True).start()

    def _render_cache_key(self, change: FileChange) -> tuple:
        return (
            change.rel,
            bool(self.var_ignore_ws.get()),
            bool(self.var_ignore_comments.get()),
            bool(self.var_wrap.get()),
            max(self.left_text.winfo_width(), 80),
            max(self.right_text.winfo_width(), 80),
        )

    def _store_render(self, key: tuple, snap: _RenderSnapshot) -> None:
        self._render_cache[key] = snap
        self._render_cache.move_to_end(key)
        while len(self._render_cache) > _RENDER_CACHE_MAX:
            self._render_cache.popitem(last=False)

    def _cols_for_width(self, width: int) -> int:
        inner = max(40, width - 20)
        return max(16, inner // max(self._mono_px, 1))

    def _align_failed(self, gen: int, rel: str, message: str) -> None:
        if gen != self._show_gen:
            return
        self.status.config(text=f"对齐失败: {rel} — {message}")

    def _align_ready(
        self,
        gen: int,
        change: FileChange,
        cache_key: tuple[str, bool, bool],
        rows: list[AlignedRow],
    ) -> None:
        self._align_cache[cache_key] = rows
        if gen != self._show_gen:
            return
        self._apply_aligned(change, rows, gen)
        self._prefetch_neighbors(change)

    def _apply_snapshot(self, change: FileChange, snap: _RenderSnapshot, gen: int) -> None:
        if gen != self._show_gen or self._current_change is not change:
            return
        self._aligned = snap.rows
        self._change_rows = list(snap.change_rows)
        self._change_cursor = 0 if self._change_rows else -1
        for text, body, tags in (
            (self.left_text, snap.left, snap.left_tags),
            (self.right_text, snap.right, snap.right_tags),
        ):
            text.configure(state=tk.NORMAL)
            text.delete("1.0", tk.END)
            text.insert("1.0", body)
            self._apply_tags(text, tags)
            text.configure(state=tk.DISABLED)
        self._draw_overview()
        if self._change_rows:
            self._goto_change(0, absolute=True)
        self.status.config(
            text=f"{change.rel} · {len(snap.rows)} 行 · {len(self._change_rows)} 处差异 · 缓存"
        )

    def _apply_aligned(self, change: FileChange, rows: list[AlignedRow], gen: int) -> None:
        if gen != self._show_gen or self._current_change is not change:
            return
        self._aligned = rows
        self._change_rows = change_row_indices(rows)
        self._change_cursor = 0 if self._change_rows else -1
        snap = self._render_side_by_side(rows)
        if snap is not None:
            self._store_render(self._render_cache_key(change), snap)
        self._draw_overview()
        if self._change_rows:
            self._goto_change(0, absolute=True)
        n = len(self._change_rows)
        self.status.config(text=f"{change.rel} · {len(rows)} 行 · {n} 处差异")

    def _build_snapshot(
        self,
        rows: list[AlignedRow],
        *,
        wrap: bool,
        left_cols: int,
        right_cols: int,
    ) -> _RenderSnapshot:
        ln_width = self._line_no_width(rows)
        left_parts: list[str] = []
        right_parts: list[str] = []
        left_tags: list[tuple[str, int, int]] = []
        right_tags: list[tuple[str, int, int]] = []
        left_pos = 0
        right_pos = 0

        for i, row in enumerate(rows):
            left_pad = 0
            right_pad = 0
            if wrap:
                left_blank = row.left is None or row.kind == "insert"
                right_blank = row.right is None or row.kind == "delete"
                left_h = self._estimate_wrap_lines(
                    None if left_blank else row.left, left_cols, ln_width
                )
                right_h = self._estimate_wrap_lines(
                    None if right_blank else row.right, right_cols, ln_width
                )
                if left_h > right_h:
                    right_pad = left_h - right_h
                elif right_h > left_h:
                    left_pad = right_h - left_h

            left_pos = self._append_side(
                left_parts,
                left_tags,
                left_pos,
                row.left,
                row.kind,
                "left",
                row.left_spans,
                i,
                row.left_no,
                ln_width,
                left_pad,
            )
            right_pos = self._append_side(
                right_parts,
                right_tags,
                right_pos,
                row.right,
                row.kind,
                "right",
                row.right_spans,
                i,
                row.right_no,
                ln_width,
                right_pad,
            )

        return _RenderSnapshot(
            left="".join(left_parts),
            right="".join(right_parts),
            left_tags=left_tags,
            right_tags=right_tags,
            change_rows=change_row_indices(rows),
            rows=rows,
        )

    def _render_side_by_side(self, rows: list[AlignedRow]) -> _RenderSnapshot | None:
        for text in (self.left_text, self.right_text):
            text.configure(state=tk.NORMAL)
            text.delete("1.0", tk.END)

        wrap = bool(self.var_wrap.get())
        left_cols = self._wrap_cols(self.left_text) if wrap else 0
        right_cols = self._wrap_cols(self.right_text) if wrap else 0
        snap = self._build_snapshot(
            rows, wrap=wrap, left_cols=left_cols, right_cols=right_cols
        )
        self.left_text.insert("1.0", snap.left)
        self.right_text.insert("1.0", snap.right)
        self._apply_tags(self.left_text, snap.left_tags)
        self._apply_tags(self.right_text, snap.right_tags)
        for text in (self.left_text, self.right_text):
            text.configure(state=tk.DISABLED)
        return snap

    @staticmethod
    def _merge_tag_ranges(
        tags: list[tuple[str, int, int]],
    ) -> list[tuple[str, int, int]]:
        if not tags:
            return []
        by_name: dict[str, list[tuple[int, int]]] = {}
        for name, a, b in tags:
            if a >= b:
                continue
            by_name.setdefault(name, []).append((a, b))
        merged: list[tuple[str, int, int]] = []
        for name, ranges in by_name.items():
            ranges.sort()
            ca, cb = ranges[0]
            for a, b in ranges[1:]:
                if a <= cb:
                    cb = max(cb, b)
                else:
                    merged.append((name, ca, cb))
                    ca, cb = a, b
            merged.append((name, ca, cb))
        return merged

    def _apply_tags(
        self, widget: tk.Text, tags: list[tuple[str, int, int]]
    ) -> None:
        for name, a, b in self._merge_tag_ranges(tags):
            widget.tag_add(name, f"1.0+{a}c", f"1.0+{b}c")

    def _wrap_cols(self, widget: tk.Text) -> int:
        width = max(widget.winfo_width(), 80)
        return self._cols_for_width(width)

    @staticmethod
    def _line_no_width(rows: list[AlignedRow]) -> int:
        biggest = 0
        for row in rows:
            if row.left_no is not None:
                biggest = max(biggest, row.left_no)
            if row.right_no is not None:
                biggest = max(biggest, row.right_no)
        return max(4, len(str(biggest or 1)))

    def _estimate_wrap_lines(
        self, content: str | None, cols: int, ln_width: int = 4
    ) -> int:
        if content is None:
            return 1
        if cols <= 0:
            return 1
        total = ln_width + 1 + len(content)
        return max(1, (total + cols - 1) // cols)

    def _append_side(
        self,
        parts: list[str],
        tags: list[tuple[str, int, int]],
        pos: int,
        content: str | None,
        kind: str,
        side: str,
        spans: list[tuple[int, int]],
        row_index: int,
        file_line_no: int | None,
        ln_width: int,
        extra_newlines: int = 0,
    ) -> int:
        # Real file line number; blank gutter when this side has no source line.
        if file_line_no is None:
            ln = " " * ln_width + " "
        else:
            ln = f"{file_line_no:>{ln_width}} "
        paint = kind
        if kind == "replace":
            paint = "delete" if side == "left" else "insert"

        blank = (
            content is None
            or (side == "left" and kind == "insert")
            or (side == "right" and kind == "delete")
        )
        start = pos
        style_change = paint in ("delete", "insert")

        if blank:
            parts.append(ln)
            parts.append("\n")
            pos += len(ln) + 1
            if style_change:
                tags.append(("empty", start, pos))
        else:
            body = content or ""
            parts.append(ln)
            body_start = pos + len(ln)
            parts.append(body)
            parts.append("\n")
            pos = body_start + len(body) + 1
            if style_change:
                line_tag = paint
                tags.append((line_tag, start, pos))
                if spans:
                    inline_tag = "inline_del" if side == "left" else "inline_add"
                    for a, b in spans:
                        tags.append((inline_tag, body_start + a, body_start + b))

        if extra_newlines > 0:
            pad_start = pos
            parts.append("\n" * extra_newlines)
            pos += extra_newlines
            if style_change:
                tags.append(("empty", pad_start, pos))

        if style_change:
            tags.append((f"r{row_index}", start, pos))
        return pos

    def _clear_diff(self) -> None:
        for text in (self.left_text, self.right_text):
            text.configure(state=tk.NORMAL)
            text.delete("1.0", tk.END)
            text.configure(state=tk.DISABLED)
        self._aligned = []
        self._change_rows = []
        self._change_cursor = -1
        self._draw_overview()

    def _set_diff_meta(self, message: str) -> None:
        for text in (self.left_text, self.right_text):
            text.configure(state=tk.NORMAL)
            text.delete("1.0", tk.END)
            text.insert(tk.END, message, "meta")
            text.configure(state=tk.DISABLED)
        self._aligned = []
        self._change_rows = []
        self._draw_overview()

    def _draw_overview(self) -> None:
        """Paint change markers + viewport on the overview strip."""
        if not hasattr(self, "overview"):
            return
        c = self.overview
        c.delete("all")
        h = max(c.winfo_height(), 1)
        w = max(c.winfo_width(), 1)
        rows = self._aligned
        if not rows:
            return

        n = len(rows)
        # Merge consecutive same-kind runs for fewer canvas items.
        i = 0
        while i < n:
            kind = rows[i].kind
            if kind == "equal":
                i += 1
                continue
            j = i + 1
            while j < n and rows[j].kind == kind:
                j += 1
            y0 = int(i * h / n)
            y1 = max(y0 + 2, int(j * h / n))
            if kind == "delete":
                fill = "#5c1818"
            elif kind == "insert":
                fill = "#144028"
            else:
                fill = "#4a3d12"
            c.create_rectangle(1, y0, w - 1, y1, outline="", fill=fill, tags="mark")
            i = j

        try:
            first, last = self.left_text.yview()
        except tk.TclError:
            first, last = 0.0, 1.0
        vy0 = float(first) * h
        vy1 = max(vy0 + 4, float(last) * h)
        c.create_rectangle(
            0, vy0, w, vy1, outline=ACCENT, width=1, fill="", tags="view"
        )

    def _on_overview_click(self, event) -> None:
        h = max(self.overview.winfo_height(), 1)
        frac = max(0.0, min(1.0, event.y / h))
        # Center viewport roughly on click.
        try:
            first, last = self.left_text.yview()
            span = max(0.05, float(last) - float(first))
        except tk.TclError:
            span = 0.1
        moveto = max(0.0, min(1.0 - span, frac - span / 2))
        self._scroll_both("moveto", str(moveto))
        self._draw_overview()

    def _goto_change(self, step: int, absolute: bool = False) -> None:
        if not self._change_rows:
            return
        if absolute:
            self._change_cursor = 0
        else:
            if self._change_cursor < 0:
                self._change_cursor = 0
            else:
                self._change_cursor = (self._change_cursor + step) % len(self._change_rows)
        row = self._change_rows[self._change_cursor]
        for text in (self.left_text, self.right_text):
            ranges = text.tag_ranges(f"r{row}")
            if ranges:
                text.see(ranges[0])
        self._draw_overview()

    def _scroll_both(self, *args) -> None:
        self.left_text.yview(*args)
        self.right_text.yview(*args)
        self._draw_overview()

    def _on_left_yscroll(self, first: str, last: str) -> None:
        if self._syncing_scroll:
            return
        self._syncing_scroll = True
        self.right_text.yview_moveto(first)
        self._syncing_scroll = False
        self._draw_overview()

    def _on_right_yscroll(self, first: str, last: str) -> None:
        if self._syncing_scroll:
            return
        self._syncing_scroll = True
        self.left_text.yview_moveto(first)
        self._syncing_scroll = False
        self._draw_overview()

    def _on_mousewheel(self, event) -> str:
        if getattr(event, "num", None) == 4 or event.delta > 0:
            delta = -1
        else:
            delta = 1
        if sys.platform == "darwin":
            delta = -1 if event.delta > 0 else 1
        self.left_text.yview_scroll(delta, "units")
        self.right_text.yview_scroll(delta, "units")
        self._draw_overview()
        return "break"

    def _active_path(self) -> str | None:
        change = self._current_change
        if not change:
            return None
        return change.path_b or change.path_a

    def _copy_path(self) -> None:
        path = self._active_path()
        if not path:
            messagebox.showinfo("提示", "请先选择一个文件")
            return
        self.clipboard_clear()
        self.clipboard_append(path)
        self.status.config(text=f"已复制路径: {path}")

    def _open_file(self) -> None:
        path = self._active_path()
        if not path or not Path(path).exists():
            messagebox.showinfo("提示", "文件不存在或未选择")
            return
        try:
            if sys.platform == "win32":
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.run(["open", path], check=False)
            else:
                subprocess.run(["xdg-open", path], check=False)
        except OSError as exc:
            messagebox.showerror("错误", str(exc))

    def _reveal_file(self) -> None:
        path = self._active_path()
        if not path or not Path(path).exists():
            messagebox.showinfo("提示", "文件不存在或未选择")
            return
        try:
            if sys.platform == "win32":
                subprocess.run(["explorer", "/select,", str(Path(path))], check=False)
            elif sys.platform == "darwin":
                subprocess.run(["open", "-R", path], check=False)
            else:
                subprocess.run(["xdg-open", str(Path(path).parent)], check=False)
        except OSError as exc:
            messagebox.showerror("错误", str(exc))

    def _export_diff(self) -> None:
        if self._busy:
            messagebox.showinfo("提示", "正在处理中，请稍后再试")
            return
        if not self._last_result or self._last_result.error:
            messagebox.showinfo("提示", "请先完成一次对比")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".diff",
            filetypes=[("Diff", "*.diff"), ("Text", "*.txt"), ("All", "*.*")],
            initialfile="compare.diff",
        )
        if not path:
            return
        result = self._last_result
        ignore_ws = bool(self.var_ignore_ws.get())
        ignore_comments = bool(self.var_ignore_comments.get())
        self._set_busy(True, "正在导出 Diff…")

        def work() -> None:
            err: str | None = None
            try:
                export_unified_diff(
                    result,
                    Path(path),
                    ignore_whitespace=ignore_ws,
                    ignore_comments=ignore_comments,
                )
            except OSError as exc:
                err = str(exc)
            except Exception as exc:
                err = str(exc)

            def done() -> None:
                self._set_busy(False)
                if err:
                    messagebox.showerror("错误", err)
                    self.status.config(text="导出失败")
                else:
                    self.status.config(
                        text=f"已导出 Diff: {path}（另存 encoding_report）"
                    )

            self.after(0, done)

        threading.Thread(target=work, daemon=True).start()

    def _export_html(self) -> None:
        if self._busy:
            messagebox.showinfo("提示", "正在处理中，请稍后再试")
            return
        if not self._last_result or self._last_result.error:
            messagebox.showinfo("提示", "请先完成一次对比")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".html",
            filetypes=[("HTML", "*.html"), ("All", "*.*")],
            initialfile="compare-report.html",
        )
        if not path:
            return
        result = self._last_result
        dir_a = self.var_a.get().strip()
        dir_b = self.var_b.get().strip()
        ignore_ws = bool(self.var_ignore_ws.get())
        ignore_comments = bool(self.var_ignore_comments.get())
        self._set_busy(True, "正在导出 HTML…")

        def work() -> None:
            err: str | None = None
            try:
                export_html_report(
                    result,
                    Path(path),
                    dir_a=dir_a,
                    dir_b=dir_b,
                    ignore_whitespace=ignore_ws,
                    ignore_comments=ignore_comments,
                )
            except OSError as exc:
                err = str(exc)
            except Exception as exc:
                err = str(exc)

            def done() -> None:
                self._set_busy(False)
                if err:
                    messagebox.showerror("错误", err)
                    self.status.config(text="导出失败")
                else:
                    self.status.config(
                        text=f"已导出 HTML: {path}（另存 encoding_report）"
                    )

            self.after(0, done)

        threading.Thread(target=work, daemon=True).start()

    def _on_close(self) -> None:
        self._persist_prefs()
        self.destroy()


def run_gui(dir_a: Path | None = None, dir_b: Path | None = None) -> None:
    app = CompareApp(dir_a, dir_b)
    app.mainloop()


if __name__ == "__main__":
    run_gui()
