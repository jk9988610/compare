#!/usr/bin/env python3
"""Tkinter GUI for directory compare."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from engine import (
    KIND_LABEL,
    AlignedRow,
    CompareResult,
    FileChange,
    align_lines,
    change_row_indices,
    compute_diff,
)
from export import export_html_report, export_unified_diff
from settings import load_settings, save_settings

BG = "#1a1f24"
PANEL = "#22282f"
INK = "#e6edf2"
MUTED = "#8b9aa6"
ACCENT = "#3d8fa0"
LINE = "#3a4550"
ENTRY = "#14191e"
DIFF_BG = "#12161b"

AUTO_REFRESH_MS = 450
KIND_FILTERS = ("全部", "新增", "删除", "修改", "修改(二进制)")
KIND_FILTER_MAP = {
    "全部": None,
    "新增": "added",
    "删除": "deleted",
    "修改": "modified",
    "修改(二进制)": "binary",
}


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


class _DirChangeHandler(FileSystemEventHandler):
    def __init__(self, app: CompareApp) -> None:
        super().__init__()
        self._app = app

    def on_any_event(self, event: FileSystemEvent) -> None:
        self._app._schedule_auto_refresh()


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
        self._auto_pending = False
        self._refresh_after_id: str | None = None
        self._observer: Observer | None = None
        self._watched: tuple[str, str] | None = None
        self._aligned: list[AlignedRow] = []
        self._change_rows: list[int] = []
        self._change_cursor = -1
        self._syncing_scroll = False
        self._current_change: FileChange | None = None

        self._ui = _ui_font(self)
        self._mono = _mono_font(self)

        self._setup_style()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind_all("<F7>", lambda e: self._goto_change(1))
        self.bind_all("<Shift-F7>", lambda e: self._goto_change(-1))

        cfg = load_settings()
        try:
            self.geometry(str(cfg.get("geometry") or "1200x760"))
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

        if "auto_refresh" in cfg:
            self.var_auto.set(bool(cfg["auto_refresh"]))
        if "ignore_whitespace" in cfg:
            self.var_ignore_ws.set(bool(cfg["ignore_whitespace"]))

        a = self.var_a.get().strip()
        b = self.var_b.get().strip()
        if a and b and Path(a).is_dir() and Path(b).is_dir():
            self.after(80, lambda: self._start_compare(manual=True))

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
            selectForeground="#0e1518",
        )
        ui = self._ui
        style.configure(".", background=BG, foreground=INK, font=ui)
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=INK, font=ui)
        style.configure("Panel.TLabel", background=PANEL, foreground=INK, font=ui)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=ui)
        style.configure("Status.TLabel", background=BG, foreground=MUTED, font=ui)
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
        style.map("TEntry", fieldbackground=[("focus", "#1c242c")])
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
            foreground="#0e1518",
            font=ui,
            padding=(14, 5),
            bordercolor=ACCENT,
        )
        style.map(
            "Accent.TButton",
            background=[("active", "#4aa3b5"), ("disabled", "#2a4a52")],
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
        style.map("TButton", background=[("active", "#4c5864"), ("pressed", "#2e3842")])
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
            background="#2b323a",
            foreground=INK,
            bordercolor=LINE,
            relief="flat",
        )
        style.map(
            "Treeview",
            background=[("selected", ACCENT)],
            foreground=[("selected", "#0e1518")],
        )
        style.map("Treeview.Heading", background=[("active", "#343c46")])
        style.configure("TPanedwindow", background=BG)
        style.configure(
            "TScrollbar",
            background="#2b323a",
            troughcolor=BG,
            arrowcolor=INK,
            bordercolor=LINE,
        )
        style.map("TScrollbar", background=[("active", "#3a4550")])

    def _build(self) -> None:
        top = ttk.Frame(self)
        top.pack(fill=tk.X, padx=16, pady=(10, 4))
        ttk.Label(top, text="目录对比", font=(self._ui[0], 16)).pack(anchor=tk.W)
        ttk.Label(
            top,
            text="并排 diff · F7 / Shift+F7 跳转差异 · 自动监听 · 可导出",
            style="Muted.TLabel",
        ).pack(anchor=tk.W, pady=(2, 6))

        paths = ttk.Frame(self)
        paths.pack(fill=tk.X, padx=16)
        self.var_a = tk.StringVar()
        self.var_b = tk.StringVar()
        self.var_auto = tk.BooleanVar(value=True)
        self.var_ignore_ws = tk.BooleanVar(value=False)
        self.var_kind = tk.StringVar(value="全部")
        self.var_query = tk.StringVar()
        self._path_row(paths, "A（旧）", self.var_a, 0)
        self._path_row(paths, "B（新）", self.var_b, 1)

        actions = ttk.Frame(self)
        actions.pack(fill=tk.X, padx=16, pady=(8, 6))
        self.btn_compare = ttk.Button(
            actions, text="开始对比", style="Accent.TButton", command=self._on_compare
        )
        self.btn_compare.pack(side=tk.LEFT)
        ttk.Button(actions, text="交换 A/B", command=self._swap_dirs).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Checkbutton(
            actions, text="自动刷新", variable=self.var_auto, command=self._on_auto_toggle
        ).pack(side=tk.LEFT, padx=(12, 0))
        ttk.Checkbutton(
            actions,
            text="忽略空白",
            variable=self.var_ignore_ws,
            command=self._on_ignore_ws_toggle,
        ).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(actions, text="导出 Diff", command=self._export_diff).pack(side=tk.RIGHT)
        ttk.Button(actions, text="导出 HTML", command=self._export_html).pack(
            side=tk.RIGHT, padx=(0, 8)
        )
        self.status = ttk.Label(actions, text="选择两个目录后开始", style="Status.TLabel")
        self.status.pack(side=tk.LEFT, padx=14)

        body = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 12))

        left = ttk.Frame(body, style="Panel.TFrame")
        right = ttk.Frame(body, style="Panel.TFrame")
        body.add(left, weight=1)
        body.add(right, weight=4)

        ttk.Label(left, text="变更文件", style="Panel.TLabel").pack(anchor=tk.W, padx=10, pady=(8, 4))
        filt = ttk.Frame(left, style="Panel.TFrame")
        filt.pack(fill=tk.X, padx=8, pady=(0, 6))
        kind = ttk.Combobox(
            filt,
            textvariable=self.var_kind,
            values=KIND_FILTERS,
            state="readonly",
            width=10,
        )
        kind.pack(side=tk.LEFT)
        kind.bind("<<ComboboxSelected>>", lambda e: self._apply_filter())
        ttk.Entry(filt, textvariable=self.var_query).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0)
        )
        self.var_query.trace_add("write", lambda *_: self._apply_filter())

        tree_wrap = ttk.Frame(left, style="Panel.TFrame")
        tree_wrap.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        scroll_y = ttk.Scrollbar(tree_wrap)
        scroll_y.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree = ttk.Treeview(
            tree_wrap,
            columns=("kind", "path"),
            show="headings",
            yscrollcommand=scroll_y.set,
            selectmode="browse",
        )
        self.tree.heading("kind", text="类型")
        self.tree.heading("path", text="路径")
        self.tree.column("kind", width=78, stretch=False, anchor=tk.CENTER)
        self.tree.column("path", width=180, stretch=True)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll_y.config(command=self.tree.yview)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Button-3>", self._on_tree_menu)

        head = ttk.Frame(right, style="Panel.TFrame")
        head.pack(fill=tk.X, padx=8, pady=(8, 4))
        ttk.Label(head, text="并排 Diff", style="Panel.TLabel").pack(side=tk.LEFT)
        ttk.Button(head, text="上一处 (Shift+F7)", command=lambda: self._goto_change(-1)).pack(
            side=tk.RIGHT
        )
        ttk.Button(head, text="下一处 (F7)", command=lambda: self._goto_change(1)).pack(
            side=tk.RIGHT, padx=(0, 6)
        )

        tools = ttk.Frame(right, style="Panel.TFrame")
        tools.pack(fill=tk.X, padx=8, pady=(0, 4))
        ttk.Button(tools, text="复制路径", command=self._copy_path).pack(side=tk.LEFT)
        ttk.Button(tools, text="打开文件", command=self._open_file).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(tools, text="在资源管理器中显示", command=self._reveal_file).pack(
            side=tk.LEFT, padx=(6, 0)
        )

        panes = ttk.Frame(right, style="Panel.TFrame")
        panes.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        panes.columnconfigure(0, weight=1)
        panes.columnconfigure(1, weight=1)
        panes.rowconfigure(1, weight=1)

        ttk.Label(panes, text="A", style="Panel.TLabel").grid(row=0, column=0, sticky=tk.W)
        ttk.Label(panes, text="B", style="Panel.TLabel").grid(row=0, column=1, sticky=tk.W, padx=(8, 0))

        self.left_text = self._make_diff_text(panes)
        self.right_text = self._make_diff_text(panes)
        self.left_text.grid(row=1, column=0, sticky="nsew", pady=(4, 0))
        self.right_text.grid(row=1, column=1, sticky="nsew", padx=(8, 0), pady=(4, 0))

        self.vscroll = ttk.Scrollbar(panes, command=self._scroll_both)
        self.vscroll.grid(row=1, column=2, sticky="ns", pady=(4, 0))
        self.left_text.configure(yscrollcommand=self._on_left_yscroll)
        self.right_text.configure(yscrollcommand=self._on_right_yscroll)

        for widget in (self.left_text, self.right_text):
            widget.bind("<MouseWheel>", self._on_mousewheel)
            widget.bind("<Button-4>", self._on_mousewheel)
            widget.bind("<Button-5>", self._on_mousewheel)

        self._menu = tk.Menu(self, tearoff=0, bg=PANEL, fg=INK, activebackground=ACCENT)
        self._menu.add_command(label="复制路径", command=self._copy_path)
        self._menu.add_command(label="打开文件", command=self._open_file)
        self._menu.add_command(label="在资源管理器中显示", command=self._reveal_file)

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
        text.tag_configure("equal", foreground="#b7c2ca")
        text.tag_configure("delete", foreground="#e88b84", background="#3a2220")
        text.tag_configure("insert", foreground="#7dce8a", background="#1c3324")
        text.tag_configure("replace", foreground="#e6edf2", background="#2a3340")
        text.tag_configure("empty", foreground="#4a5560", background="#161b20")
        text.tag_configure("inline_del", background="#7a3030", foreground="#ffe4e1")
        text.tag_configure("inline_add", background="#2f6b3a", foreground="#e6ffe9")
        text.tag_configure("current", background="#3d4f5c")
        text.tag_configure("meta", foreground=MUTED)
        text.tag_configure("ln", foreground="#5a6670")
        return text

    def _path_row(self, parent: ttk.Frame, label: str, var: tk.StringVar, row: int) -> None:
        ttk.Label(parent, text=label, width=8).grid(row=row, column=0, sticky=tk.W, pady=4)
        ttk.Entry(parent, textvariable=var).grid(row=row, column=1, sticky=tk.EW, padx=8, pady=4)
        ttk.Button(parent, text="浏览…", command=lambda: self._browse(var)).grid(
            row=row, column=2, pady=4
        )
        parent.columnconfigure(1, weight=1)

    def _browse(self, var: tk.StringVar) -> None:
        initial = var.get().strip() or None
        path = filedialog.askdirectory(initialdir=initial)
        if path:
            var.set(path)
            self._stop_watch()
            self._persist_prefs()

    def _swap_dirs(self) -> None:
        a, b = self.var_a.get(), self.var_b.get()
        self.var_a.set(b)
        self.var_b.set(a)
        self._stop_watch()
        self._persist_prefs()
        if a.strip() and b.strip():
            self._start_compare(manual=True)

    def _on_auto_toggle(self) -> None:
        self._persist_prefs()
        if self.var_auto.get():
            self._start_watch_if_ready()
        else:
            self._stop_watch()

    def _on_ignore_ws_toggle(self) -> None:
        self._persist_prefs()
        if self.var_a.get().strip() and self.var_b.get().strip():
            self._start_compare(manual=True)

    def _persist_prefs(self) -> None:
        save_settings(
            {
                "dir_a": self.var_a.get().strip(),
                "dir_b": self.var_b.get().strip(),
                "auto_refresh": bool(self.var_auto.get()),
                "ignore_whitespace": bool(self.var_ignore_ws.get()),
                "geometry": self.geometry(),
            }
        )

    def _on_compare(self) -> None:
        self._start_compare(manual=True)

    def _start_compare(self, *, manual: bool, preserve_rel: str | None = None) -> None:
        if self._busy:
            if not manual:
                self._auto_pending = True
            return
        a = self.var_a.get().strip()
        b = self.var_b.get().strip()
        if not a or not b:
            if manual:
                messagebox.showwarning("提示", "请先选择两个目录")
            return
        if preserve_rel is None and not manual:
            preserve_rel = self._selected_rel()
        self._busy = True
        self.btn_compare.state(["disabled"])
        self.status.config(text="正在对比…" if manual else "检测到变更，正在刷新…")
        ignore_ws = bool(self.var_ignore_ws.get())
        threading.Thread(
            target=self._run_compare,
            args=(Path(a), Path(b), manual, preserve_rel, ignore_ws),
            daemon=True,
        ).start()

    def _run_compare(
        self,
        dir_a: Path,
        dir_b: Path,
        manual: bool,
        preserve_rel: str | None,
        ignore_ws: bool,
    ) -> None:
        try:
            result = compute_diff(dir_a, dir_b, ignore_whitespace=ignore_ws)
        except Exception as exc:
            result = CompareResult(error=str(exc))
        self.after(0, self._apply_result, result, manual, preserve_rel)

    def _apply_result(
        self,
        result: CompareResult,
        manual: bool = True,
        preserve_rel: str | None = None,
    ) -> None:
        self._busy = False
        self.btn_compare.state(["!disabled"])
        self._clear_diff()
        self._all_changes = []
        self._filtered = []
        self._last_result = None
        self._current_change = None

        if result.error:
            self.status.config(text=f"错误: {result.error}")
            if manual:
                messagebox.showerror("错误", result.error)
            if self._auto_pending:
                self._auto_pending = False
                self._start_compare(manual=False)
            return

        self._last_result = result
        self._all_changes = result.changes
        self._persist_prefs()
        if self.var_auto.get():
            self._start_watch_if_ready()
        else:
            self._stop_watch()

        self._apply_filter(preserve_rel=preserve_rel)

        suffix = " · 监听中" if self.var_auto.get() else ""
        ws = " · 忽略空白" if self.var_ignore_ws.get() else ""
        if not result.changes:
            self.status.config(text=f"两个目录内容一致（在忽略规则下）{ws}{suffix}")
        else:
            self.status.config(
                text=(
                    f"变更 {len(result.changes)} 个文件 · "
                    f"仅 A={result.only_a} · 仅 B={result.only_b} · 共同={result.common}"
                    f"{ws}{suffix}"
                )
            )

        if self._auto_pending:
            self._auto_pending = False
            self._start_compare(manual=False)

    def _apply_filter(self, preserve_rel: str | None = None) -> None:
        if preserve_rel is None:
            preserve_rel = self._selected_rel()
        kind_key = KIND_FILTER_MAP.get(self.var_kind.get(), None)
        q = self.var_query.get().strip().lower()
        filtered: list[FileChange] = []
        for change in self._all_changes:
            if kind_key and change.kind != kind_key:
                continue
            if q and q not in change.rel.lower():
                continue
            filtered.append(change)
        self._filtered = filtered

        self.tree.delete(*self.tree.get_children())
        for i, change in enumerate(filtered):
            self.tree.insert(
                "",
                tk.END,
                iid=str(i),
                values=(KIND_LABEL[change.kind], change.rel),
            )

        if not filtered:
            self._current_change = None
            self._clear_diff()
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
            self._show_change(self._filtered[idx])

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
        if change.kind == "binary":
            self._clear_diff()
            self._set_diff_meta("二进制文件不同\n" + "\n".join(change.diff_lines))
            self._aligned = []
            self._change_rows = []
            self._change_cursor = -1
            return

        ignore_ws = bool(self.var_ignore_ws.get())
        rows = align_lines(change.lines_a, change.lines_b, ignore_ws)
        self._aligned = rows
        self._change_rows = change_row_indices(rows)
        self._change_cursor = 0 if self._change_rows else -1
        self._render_side_by_side(rows)
        if self._change_rows:
            self._goto_change(0, absolute=True)

    def _render_side_by_side(self, rows: list[AlignedRow]) -> None:
        for text in (self.left_text, self.right_text):
            text.configure(state=tk.NORMAL)
            text.delete("1.0", tk.END)

        for i, row in enumerate(rows):
            tag = f"r{i}"
            self._insert_side(self.left_text, row.left, row.kind, "left", row.left_spans, tag, i)
            self._insert_side(self.right_text, row.right, row.kind, "right", row.right_spans, tag, i)

        for text in (self.left_text, self.right_text):
            text.configure(state=tk.DISABLED)

    def _insert_side(
        self,
        widget: tk.Text,
        content: str | None,
        kind: str,
        side: str,
        spans: list[tuple[int, int]],
        row_tag: str,
        row_index: int,
    ) -> None:
        ln = f"{row_index + 1:>4} "
        if content is None:
            widget.insert(tk.END, ln, ("ln", row_tag, "empty"))
            widget.insert(tk.END, "\n", ("empty", row_tag))
            return

        if side == "left" and kind == "insert":
            widget.insert(tk.END, ln, ("ln", row_tag, "empty"))
            widget.insert(tk.END, "\n", ("empty", row_tag))
            return
        if side == "right" and kind == "delete":
            widget.insert(tk.END, ln, ("ln", row_tag, "empty"))
            widget.insert(tk.END, "\n", ("empty", row_tag))
            return

        line_tag = kind if kind in ("equal", "delete", "insert", "replace") else "equal"
        widget.insert(tk.END, ln, ("ln", row_tag, line_tag))
        body_start = widget.index("end-1c")
        widget.insert(tk.END, content, (line_tag, row_tag))
        widget.insert(tk.END, "\n", (line_tag, row_tag))

        inline_tag = "inline_del" if side == "left" else "inline_add"
        for a, b in spans:
            s = f"{body_start}+{a}c"
            e = f"{body_start}+{b}c"
            widget.tag_add(inline_tag, s, e)

    def _clear_diff(self) -> None:
        for text in (self.left_text, self.right_text):
            text.configure(state=tk.NORMAL)
            text.delete("1.0", tk.END)
            text.configure(state=tk.DISABLED)
        self._aligned = []
        self._change_rows = []
        self._change_cursor = -1

    def _set_diff_meta(self, message: str) -> None:
        for text in (self.left_text, self.right_text):
            text.configure(state=tk.NORMAL)
            text.delete("1.0", tk.END)
            text.insert(tk.END, message, "meta")
            text.configure(state=tk.DISABLED)

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
            text.tag_remove("current", "1.0", tk.END)
            ranges = text.tag_ranges(f"r{row}")
            if ranges:
                text.tag_add("current", ranges[0], ranges[1])
                text.see(ranges[0])

    def _scroll_both(self, *args) -> None:
        self.left_text.yview(*args)
        self.right_text.yview(*args)

    def _on_left_yscroll(self, first: str, last: str) -> None:
        self.vscroll.set(first, last)
        if self._syncing_scroll:
            return
        self._syncing_scroll = True
        self.right_text.yview_moveto(first)
        self._syncing_scroll = False

    def _on_right_yscroll(self, first: str, last: str) -> None:
        self.vscroll.set(first, last)
        if self._syncing_scroll:
            return
        self._syncing_scroll = True
        self.left_text.yview_moveto(first)
        self._syncing_scroll = False

    def _on_mousewheel(self, event) -> str:
        if getattr(event, "num", None) == 4 or event.delta > 0:
            delta = -1
        else:
            delta = 1
        if sys.platform == "darwin":
            delta = -1 if event.delta > 0 else 1
        self.left_text.yview_scroll(delta, "units")
        self.right_text.yview_scroll(delta, "units")
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
        try:
            export_unified_diff(self._last_result, Path(path))
            self.status.config(text=f"已导出 Diff: {path}")
        except OSError as exc:
            messagebox.showerror("错误", str(exc))

    def _export_html(self) -> None:
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
        try:
            export_html_report(
                self._last_result,
                Path(path),
                dir_a=self.var_a.get().strip(),
                dir_b=self.var_b.get().strip(),
                ignore_whitespace=bool(self.var_ignore_ws.get()),
            )
            self.status.config(text=f"已导出 HTML: {path}")
        except OSError as exc:
            messagebox.showerror("错误", str(exc))

    def _schedule_auto_refresh(self) -> None:
        if not self.var_auto.get():
            return
        self.after(0, self._debounce_auto_refresh)

    def _debounce_auto_refresh(self) -> None:
        if self._refresh_after_id is not None:
            try:
                self.after_cancel(self._refresh_after_id)
            except tk.TclError:
                pass
        self._refresh_after_id = self.after(AUTO_REFRESH_MS, self._do_auto_refresh)

    def _do_auto_refresh(self) -> None:
        self._refresh_after_id = None
        if not self.var_auto.get():
            return
        self._start_compare(manual=False)

    def _start_watch_if_ready(self) -> None:
        if not self.var_auto.get():
            return
        a = self.var_a.get().strip()
        b = self.var_b.get().strip()
        if not a or not b:
            return
        pa, pb = Path(a), Path(b)
        if not pa.is_dir() or not pb.is_dir():
            return
        key = (str(pa.resolve()), str(pb.resolve()))
        if self._observer is not None and self._watched == key:
            return
        self._stop_watch()
        handler = _DirChangeHandler(self)
        observer = Observer()
        try:
            observer.schedule(handler, key[0], recursive=True)
            observer.schedule(handler, key[1], recursive=True)
            observer.start()
        except Exception as exc:
            self.status.config(text=f"无法监听目录: {exc}")
            return
        self._observer = observer
        self._watched = key

    def _stop_watch(self) -> None:
        if self._refresh_after_id is not None:
            try:
                self.after_cancel(self._refresh_after_id)
            except tk.TclError:
                pass
            self._refresh_after_id = None
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=2)
            except Exception:
                pass
            self._observer = None
            self._watched = None

    def _on_close(self) -> None:
        self._persist_prefs()
        self._stop_watch()
        self.destroy()


def run_gui(dir_a: Path | None = None, dir_b: Path | None = None) -> None:
    app = CompareApp(dir_a, dir_b)
    app.mainloop()


if __name__ == "__main__":
    run_gui()
