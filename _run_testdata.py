"""Smoke-test encoding unify + compute_diff on testdata_diff."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, r"E:\tools")

from encoding_bridge import prepare_for_diff  # noqa: E402
from engine import compute_diff  # noqa: E402


def summarize(label: str, result) -> dict:
    kinds: dict[str, list[str]] = {}
    for c in result.changes:
        kinds.setdefault(c.kind, []).append(c.rel)
    print(f"\n=== {label} ===")
    print(f"only_a={result.only_a} only_b={result.only_b} common={result.common}")
    print(f"changes={len(result.changes)}")
    for kind, rels in sorted(kinds.items()):
        print(f"  [{kind}] {len(rels)}: {', '.join(rels)}")
    return {k: sorted(v) for k, v in kinds.items()}


def check_utf8_lf(root: Path) -> list[str]:
    problems: list[str] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in {".c", ".h", ".ini", ".txt"}:
            continue
        if "_encoding_backup" in p.parts:
            continue
        data = p.read_bytes()
        if data.startswith(b"\xef\xbb\xbf"):
            problems.append(f"BOM still: {p.relative_to(root)}")
        if b"\r" in data:
            problems.append(f"CR still: {p.relative_to(root)}")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            problems.append(f"not utf-8: {p.relative_to(root)}")
    return problems


def main() -> int:
    old = ROOT / "testdata_diff" / "old"
    new = ROOT / "testdata_diff" / "new"
    if not old.is_dir() or not new.is_dir():
        print("missing testdata; run _make_testdata.py first")
        return 1

    before = compute_diff(old, new)
    kb = summarize("BEFORE unify", before)

    prep = prepare_for_diff(old, new, apply=True)
    print("\n=== prepare_for_diff(apply=True) ===")
    print(prep.text())

    after = compute_diff(old, new)
    ka = summarize("AFTER unify", after)

    old_probs = check_utf8_lf(old)
    new_probs = check_utf8_lf(new)
    print("\n=== disk UTF-8+LF check ===")
    print("old problems:", old_probs or "none")
    print("new problems:", new_probs or "none")

    # Expectations after unify
    ok = True
    # util.c should no longer be encoding-only
    enc_after = set(ka.get("encoding", []))
    if "src/util.c" in enc_after:
        print("FAIL: util.c still encoding-only after unify")
        ok = False
    else:
        print("OK: util.c encoding-only cleared")

    # business diffs should remain
    for rel in ("src/main.c", "inc/main.h", "config.ini"):
        if rel not in ka.get("modified", []):
            # config might show as modified
            print(f"WARN: expected modified {rel}, got kinds {[k for k,v in ka.items() if rel in v]}")
    if "src/main.c" not in ka.get("modified", []):
        print("FAIL: main.c business diff missing")
        ok = False
    else:
        print("OK: main.c still modified")

    if "src/watchdog.c" not in ka.get("added", []):
        print("FAIL: watchdog.c added missing")
        ok = False
    else:
        print("OK: watchdog.c added")

    if "src/legacy_dbg.c" not in ka.get("deleted", []):
        print("FAIL: legacy_dbg.c deleted missing")
        ok = False
    else:
        print("OK: legacy_dbg.c deleted")

    if old_probs or new_probs:
        print("FAIL: residual BOM/CR/non-utf8")
        ok = False
    else:
        print("OK: scanned text files are UTF-8 no BOM + no CR")

    # Unicode content of util should match before (encoding-only) and disappear after
    if "src/util.c" not in kb.get("encoding", []):
        print("WARN: before unify, util.c was not encoding-only:", kb)
    else:
        print("OK: before unify, util.c was encoding-only (eol)")

    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
