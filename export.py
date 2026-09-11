"""Export compare results to unified diff or HTML."""

from __future__ import annotations

import html
from pathlib import Path

from engine import KIND_LABEL, CompareResult, FileChange, align_lines, build_unified_report


def export_unified_diff(result: CompareResult, path: Path) -> None:
    path.write_text(build_unified_report(result), encoding="utf-8")


def _span_html(text: str, spans: list[tuple[int, int]], mark_class: str) -> str:
    if not text and text != "":
        return ""
    if not spans:
        return html.escape(text)
    parts: list[str] = []
    cursor = 0
    for start, end in spans:
        if start > cursor:
            parts.append(html.escape(text[cursor:start]))
        parts.append(f'<mark class="{mark_class}">{html.escape(text[start:end])}</mark>')
        cursor = end
    if cursor < len(text):
        parts.append(html.escape(text[cursor:]))
    return "".join(parts)


def _rows_table(rows: list[AlignedRow]) -> str:
    body: list[str] = []
    for i, row in enumerate(rows):
        cls = row.kind
        left = "" if row.left is None else _span_html(row.left, row.left_spans, "inline-del")
        right = "" if row.right is None else _span_html(row.right, row.right_spans, "inline-add")
        left_cell = "&nbsp;" if row.left is None else left
        right_cell = "&nbsp;" if row.right is None else right
        body.append(
            f'<tr class="{cls}" id="r{i}">'
            f'<td class="ln">{i + 1}</td>'
            f'<td class="code left">{left_cell}</td>'
            f'<td class="code right">{right_cell}</td>'
            f"</tr>"
        )
    return "\n".join(body)


def _file_section(change: FileChange, ignore_whitespace: bool) -> str:
    label = KIND_LABEL[change.kind]
    if change.kind == "binary":
        note = html.escape("\n".join(change.diff_lines))
        return (
            f'<section class="file" id="{html.escape(change.rel)}">'
            f"<h2>{html.escape(label)} · {html.escape(change.rel)}</h2>"
            f'<pre class="note">{note}</pre></section>'
        )
    rows = align_lines(change.lines_a, change.lines_b, ignore_whitespace)
    return (
        f'<section class="file" id="{html.escape(change.rel)}">'
        f"<h2>{html.escape(label)} · {html.escape(change.rel)}</h2>"
        f'<table class="diff"><thead><tr>'
        f'<th class="ln">#</th><th>A</th><th>B</th>'
        f"</tr></thead><tbody>\n{_rows_table(rows)}\n</tbody></table></section>"
    )


def export_html_report(
    result: CompareResult,
    path: Path,
    *,
    dir_a: str = "",
    dir_b: str = "",
    ignore_whitespace: bool = False,
) -> None:
    nav = "\n".join(
        f'<li><a href="#{html.escape(c.rel)}">'
        f"{html.escape(KIND_LABEL[c.kind])} {html.escape(c.rel)}</a></li>"
        for c in result.changes
    )
    sections = "\n".join(_file_section(c, ignore_whitespace) for c in result.changes)
    if not result.changes:
        sections = "<p class='empty'>两个目录内容一致（在忽略规则下）。</p>"

    doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>目录对比报告</title>
<style>
:root {{
  --bg:#1a1f24; --panel:#22282f; --ink:#e6edf2; --muted:#8b9aa6;
  --accent:#3d8fa0; --line:#3a4550; --add:#1c3324; --del:#3a2220;
  --add-fg:#7dce8a; --del-fg:#e88b84;
}}
* {{ box-sizing:border-box; }}
body {{
  margin:0; font-family:"Segoe UI","Microsoft YaHei UI",sans-serif;
  background:var(--bg); color:var(--ink); line-height:1.45;
}}
header {{
  padding:20px 24px; border-bottom:1px solid var(--line);
  background:var(--panel); position:sticky; top:0; z-index:2;
}}
h1 {{ margin:0 0 6px; font-size:1.35rem; font-weight:650; }}
.meta {{ color:var(--muted); font-size:0.9rem; }}
main {{ display:grid; grid-template-columns:260px 1fr; min-height:calc(100vh - 88px); }}
nav {{
  border-right:1px solid var(--line); padding:16px; background:#1e242b;
  position:sticky; top:88px; align-self:start; max-height:calc(100vh - 88px); overflow:auto;
}}
nav h3 {{ margin:0 0 10px; font-size:0.85rem; color:var(--muted); font-weight:600; }}
nav ul {{ list-style:none; margin:0; padding:0; }}
nav li {{ margin:0 0 6px; }}
nav a {{ color:var(--ink); text-decoration:none; font-size:0.86rem; word-break:break-all; }}
nav a:hover {{ color:var(--accent); }}
.content {{ padding:20px 24px 48px; }}
.file {{
  background:var(--panel); border:1px solid var(--line); border-radius:8px;
  margin:0 0 20px; overflow:hidden;
}}
.file h2 {{
  margin:0; padding:12px 14px; font-size:0.95rem; border-bottom:1px solid var(--line);
  background:#2b323a;
}}
table.diff {{ width:100%; border-collapse:collapse; font-family:Consolas,"Cascadia Mono",monospace; font-size:12px; }}
table.diff th, table.diff td {{ vertical-align:top; padding:2px 8px; border-bottom:1px solid #2a3138; }}
table.diff th {{ text-align:left; color:var(--muted); font-weight:600; background:#1e242b; }}
td.ln {{ width:3rem; color:var(--muted); text-align:right; user-select:none; }}
td.code {{ white-space:pre-wrap; word-break:break-word; width:50%; }}
tr.delete td.left, tr.replace td.left {{ background:var(--del); color:var(--del-fg); }}
tr.insert td.right, tr.replace td.right {{ background:var(--add); color:var(--add-fg); }}
tr.delete td.right, tr.insert td.left {{ background:#161b20; color:#5a6670; }}
mark.inline-del {{ background:#7a3030; color:#ffe4e1; padding:0 1px; }}
mark.inline-add {{ background:#2f6b3a; color:#e6ffe9; padding:0 1px; }}
.note {{ margin:0; padding:14px; color:var(--muted); }}
.empty {{ color:var(--muted); }}
@media (max-width:900px) {{
  main {{ grid-template-columns:1fr; }}
  nav {{ position:static; max-height:none; }}
}}
</style>
</head>
<body>
<header>
  <h1>目录对比报告</h1>
  <div class="meta">
    A: {html.escape(dir_a) or "—"}<br/>
    B: {html.escape(dir_b) or "—"}<br/>
    变更 {len(result.changes)} · 仅 A={result.only_a} · 仅 B={result.only_b} · 共同={result.common}
    {" · 已忽略空白" if ignore_whitespace else ""}
  </div>
</header>
<main>
  <nav>
    <h3>文件</h3>
    <ul>{nav or "<li class='empty'>无变更</li>"}</ul>
  </nav>
  <div class="content">
    {sections}
  </div>
</main>
</body>
</html>
"""
    path.write_text(doc, encoding="utf-8")


__all__ = [
    "export_unified_diff",
    "export_html_report",
]
