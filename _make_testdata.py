"""Build old/new fixture trees under testdata_diff/ for encoding + Diff smoke tests."""
from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "testdata_diff"


def write(path: Path, text: str, encoding: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode(encoding))


def main() -> None:
    if ROOT.exists():
        shutil.rmtree(ROOT)
    old = ROOT / "old"
    new = ROOT / "new"
    old.mkdir(parents=True)
    new.mkdir(parents=True)

    # --- old: mixed charset / EOL ---
    write(
        old / "src" / "main.c",
        "#include \"main.h\"\r\n"
        "#include <stdio.h>\r\n"
        "\r\n"
        "// 老版本主循环\r\n"
        "int main(void)\r\n"
        "{\r\n"
        "    int counter = 0;\r\n"
        "    while (1) {\r\n"
        "        counter++;\r\n"
        "        sleep_ms(10);\r\n"
        "    }\r\n"
        "    return 0;\r\n"
        "}\r\n",
        "gbk",
    )
    write(
        old / "inc" / "main.h",
        "#ifndef MAIN_H\r\n"
        "#define MAIN_H\r\n"
        "\r\n"
        "// 延时接口\r\n"
        "void sleep_ms(int ms);\r\n"
        "\r\n"
        "#endif\r\n",
        "gbk",
    )
    write(
        old / "src" / "util.c",
        "// util\r\n"
        "int add(int a, int b)\r\n"
        "{\r\n"
        "    return a + b;\r\n"
        "}\r\n",
        "utf-8",
    )
    write(old / "config.ini", "\ufeffkey=value\r\nmode=legacy\r\n", "utf-8-sig")
    write(old / "readme.txt", "# readme\nplain text\n", "utf-8")
    write(
        old / "src" / "legacy_dbg.c",
        "// 仅旧侧调试\r\nvoid dbg_dump(void) {}\r\n",
        "gbk",
    )

    # --- new: copy old then apply business + encoding-only edits ---
    shutil.copytree(old, new, dirs_exist_ok=True)

    write(
        new / "src" / "main.c",
        "#include \"main.h\"\r\n"
        "#include <stdio.h>\r\n"
        "\r\n"
        "// 新版本主循环：增加初始化\r\n"
        "int main(void)\r\n"
        "{\r\n"
        "    int counter = 0;\r\n"
        "    init_board();\r\n"
        "    while (1) {\r\n"
        "        counter++;\r\n"
        "        sleep_ms(10);\r\n"
        "        kick_watchdog();\r\n"
        "    }\r\n"
        "    return 0;\r\n"
        "}\r\n",
        "gbk",
    )
    write(
        new / "inc" / "main.h",
        "#ifndef MAIN_H\r\n"
        "#define MAIN_H\r\n"
        "\r\n"
        "// 延时接口\r\n"
        "void sleep_ms(int ms);\r\n"
        "void init_board(void);\r\n"
        "void kick_watchdog(void);\r\n"
        "\r\n"
        "#endif\r\n",
        "gbk",
    )
    # same Unicode as old util, but LF only → encoding-only (eol)
    write(
        new / "src" / "util.c",
        "// util\n"
        "int add(int a, int b)\n"
        "{\n"
        "    return a + b;\n"
        "}\n",
        "utf-8",
    )
    write(new / "config.ini", "key=value\nmode=modern\n", "utf-8")
    (new / "src" / "legacy_dbg.c").unlink()
    write(
        new / "src" / "watchdog.c",
        "// 看门狗\r\nvoid kick_watchdog(void) {}\r\n",
        "gbk",
    )

    print(f"created {ROOT}")
    for p in sorted(ROOT.rglob("*")):
        if p.is_file():
            print(f"  {p.relative_to(ROOT)} ({p.stat().st_size} B)")


if __name__ == "__main__":
    main()
