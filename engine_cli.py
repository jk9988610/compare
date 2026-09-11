#!/usr/bin/env python3
"""JSON CLI for cedit: print CompareResult as JSON."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from engine import compute_diff


def main() -> int:
    parser = argparse.ArgumentParser(description="Directory compare JSON for cedit")
    parser.add_argument("dir_a", type=Path)
    parser.add_argument("dir_b", type=Path)
    parser.add_argument("--ignore-whitespace", action="store_true")
    args = parser.parse_args()

    result = compute_diff(
        args.dir_a, args.dir_b, ignore_whitespace=args.ignore_whitespace
    )
    payload = {
        "error": result.error,
        "only_a": result.only_a,
        "only_b": result.only_b,
        "common": result.common,
        "changes": [
            {
                "rel": c.rel,
                "kind": c.kind,
                "diff_lines": c.diff_lines,
                "lines_a": c.lines_a,
                "lines_b": c.lines_b,
                "path_a": c.path_a,
                "path_b": c.path_b,
            }
            for c in result.changes
        ],
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.write("\n")
    return 0 if not result.error else 2


if __name__ == "__main__":
    raise SystemExit(main())
