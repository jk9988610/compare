# compare

对比两个目录中所有**非忽略**文件的内容，GUI 默认**并排 diff**；CLI 输出标准 **unified diff**。

## 安装

```bash
pip install -r requirements.txt
```

## 用法

```bash
python compare.py
python compare.py --gui
python compare.py <目录A> <目录B>
python compare.py <目录A> <目录B> --ignore-whitespace
python compare.py <目录A> <目录B> --no-color
```

### GUI 能力

- 记住上次目录 / 窗口 / 自动刷新 / 忽略空白
- 自动监听刷新
- 并排 diff、行内字符高亮、F7 / Shift+F7 跳转差异
- 交换 A/B、类型过滤与路径搜索
- 复制路径、打开文件、资源管理器中显示
- 导出 `.diff` / HTML 报告

## 依赖

- Python 3.10+
- `pathspec`、`colorama`、`watchdog`
