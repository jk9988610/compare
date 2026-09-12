#!/usr/bin/env python3
"""Compare two directories with unified diff, respecting .gitignore."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from colorama import Fore, Style, init as colorama_init

from engine import KIND_LABEL, compute_diff


def colorize_diff_line(line: str, use_color: bool) -> str:
    if not use_color:
        return line
    if line.startswith("+++") or line.startswith("---"):
        return f"{Style.BRIGHT}{line}{Style.RESET_ALL}"
    if line.startswith("@@"):
        return f"{Fore.CYAN}{line}{Style.RESET_ALL}"
    if line.startswith("+"):
        return f"{Fore.GREEN}{line}{Style.RESET_ALL}"
    if line.startswith("-"):
        return f"{Fore.RED}{line}{Style.RESET_ALL}"
    return line


def print_header(title: str, use_color: bool) -> None:
    bar = "=" * 60
    if use_color:
        print(f"\n{Fore.YELLOW}{bar}{Style.RESET_ALL}")
        print(f"{Fore.YELLOW}{title}{Style.RESET_ALL}")
        print(f"{Fore.YELLOW}{bar}{Style.RESET_ALL}")
    else:
        print(f"\n{bar}")
        print(title)
        print(bar)


def compare_dirs(
    dir_a: Path,
    dir_b: Path,
    use_color: bool,
    ignore_whitespace: bool = False,
    ignore_comments: bool = False,
) -> int:
    result = compute_diff(
        dir_a,
        dir_b,
        ignore_whitespace=ignore_whitespace,
        ignore_comments=ignore_comments,
    )
    if result.error:
        print(f"错误: {result.error}", file=sys.stderr)
        return 2

    if not result.changes:
        print("两个目录内容一致（在忽略规则下）。")
        return 0

    for change in result.changes:
        print_header(f"{KIND_LABEL[change.kind]}: {change.rel}", use_color)
        for line in change.diff_lines:
            print(colorize_diff_line(line, use_color))

    print()
    print(
        f"摘要: 仅 A={result.only_a}, 仅 B={result.only_b}, "
        f"共同={result.common}"
    )
    return 1


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description="对比两个目录的非忽略文件，输出标准 unified diff（+/-）。"
    )
    parser.add_argument("dir_a", nargs="?", type=Path, help="左侧/旧目录")
    parser.add_argument("dir_b", nargs="?", type=Path, help="右侧/新目录")
    parser.add_argument("--gui", action="store_true", help="打开图形界面")
    parser.add_argument("--no-color", action="store_true", help="禁用终端颜色")
    parser.add_argument(
        "--ignore-whitespace",
        action="store_true",
        help="忽略空白差异（压缩行内空白，并忽略空行增删）",
    )
    parser.add_argument(
        "--ignore-comments",
        action="store_true",
        help="忽略注释差异（按扩展名启发式剥离注释后比较）",
    )
    args = parser.parse_args()

    if args.gui or (args.dir_a is None and args.dir_b is None):
        from gui import run_gui

        run_gui(args.dir_a, args.dir_b)
        return 0

    if args.dir_a is None or args.dir_b is None:
        parser.error("请同时提供两个目录，或使用 --gui / 不带参数启动图形界面")

    use_color = not args.no_color and sys.stdout.isatty()
    if use_color:
        colorama_init(autoreset=False)

    return compare_dirs(
        args.dir_a,
        args.dir_b,
        use_color,
        ignore_whitespace=args.ignore_whitespace,
        ignore_comments=args.ignore_comments,
    )


if __name__ == "__main__":
    raise SystemExit(main())
