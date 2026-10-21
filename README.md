# user-ui（CrMcDownload 客户端·界面）

tkinter 界面：

- `offcraft/ui/theme.py`：深色主题与配色
- `offcraft/ui/widgets.py`：通用控件
- `offcraft/ui/app.py`：主窗口与页面切换
- `offcraft/ui/pages/`：下载页、实例页、设置页、日志页

## 开发

```bash
uv sync --frozen
uv run python -m unittest discover -s tests -v
```

合并方向 `user-ui → user`。
