# compare

对比两个目录中所有**非忽略**文件的内容，GUI 默认**并排 diff**；CLI 输出标准 **unified diff**。

## 安装

```bash
pip install -r requirements.txt
```

开发 / 打包额外依赖：

```bash
pip install -r requirements-dev.txt
```

## 用法

```bash
python compare.py
python compare.py --gui
python compare.py <目录A> <目录B>
python compare.py <目录A> <目录B> --ignore-whitespace
python compare.py <目录A> <目录B> --ignore-comments
python compare.py <目录A> <目录B> --no-color
python engine_cli.py <目录A> <目录B>   # JSON 输出，供 Easycode 调用
```

### GUI 能力

- 记住上次目录 / 窗口 / 侧栏显隐 / 忽略空白（含空行） / 忽略注释 / 自定义忽略规则
- 配置写入 `%APPDATA%\目录对比\settings.json`（安装目录只读也可写配置；首次可从旧旁路 `settings.json` 迁移）
- 自动识别语言（扩展名 + 内容嗅探）以改进注释剥离
- 顶栏「编码检查」内嵌 `E:\tools\encoding_unify`（对当前旧侧/新侧检查，可统一为 UTF-8 无 BOM + LF）
- Diff **只读**对比；统一写盘由「编码检查」调用 unify（带 `_encoding_backup`）；合入由 Cursor/脚本按内容匹配
- 下游说明见 [`给下游Cursor_使用说明.md`](给下游Cursor_使用说明.md)
- 自动检测文件编码（严格 UTF-8 → GBK/GB18030，禁止 `errors=replace` 写入 diff；换行统一为 `\n`）
- 导出 Diff/HTML 时旁路生成 `*.encoding_report.txt`；unified diff 头注明 `# encoding: utf-8` 与左右 `encoding -> unicode`
- 「仅编码」变更：Unicode 相同但字节不同（EOL/BOM/编码）；导出带 `# encoding-only-reason`
- GUI **默认隐藏「仅编码」**（底栏勾选「仅编码」才显示）；状态栏仍提示隐藏数量
- 并排 diff 底色接近 Git：红删 / 绿增；同行修改左右对照，仅增删行才对面留空
- 扫描进度状态栏 `正在扫描 N/M：path…`；导出 Diff/HTML 在后台线程，界面不假死
- 点击「运行」扫描刷新（无目录监听）
- 并排 diff、行内字符高亮、F7 / Shift+F7 跳转差异
- 交换 A/B、文件名搜索、「规则」卡片式忽略（目录选择 + 类型下拉）
- 复制路径、打开文件、资源管理器中显示
- Windows：Cursor 风格深色壳层、自绘顶栏（拖动 / 双击最大化 / 最小化关闭 / 侧栏显隐）
- 导出 `.diff` / HTML 报告（勾选「注释」时：仅注释改写不打 `+/-`，新增/删除注释行仍会输出）

「忽略注释」只隐藏「仅注释内容被改写」的差异（例如 `// a`→`// b`，或同行尾注释改写而代码不变）。**新增或删除的整行注释仍会显示为差异。** 按语言启发式剥离（`#` / `//` / `/* */` / `--` / `<!-- -->`）。同行既改代码又改注释时，仍按代码判定差异，行内高亮只标代码段。不是完整语法解析。

## 打包 Windows 客户端

```bash
build.bat
```

或：

```bash
pip install -r requirements.txt -r requirements-dev.txt
pyinstaller --noconfirm compare.spec
```

产物目录：`dist\目录对比\`，双击其中的 `目录对比.exe` 即可运行（onedir，无控制台窗口）。配置仍写入 `%APPDATA%\目录对比\`。

## 依赖

- Python 3.10+
- `pathspec`、`colorama`、`charset-normalizer`
- 打包：`pyinstaller`（见 `requirements-dev.txt`）
