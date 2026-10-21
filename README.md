# user-downloader（CrMcDownload 客户端·下载与安装）

负责“把游戏弄到本地”的全部逻辑：

- `offcraft/mirror.py`：上游 URL → 镜像地址重写（meta/data/libs/assets/fabric）
- `offcraft/net/`：SOCKS5 / HTTP CONNECT 代理、并发下载、断点续传、SHA-1 校验、进度与取消
- `offcraft/meta/`：版本清单、版本 JSON、`inheritsFrom` 合并、`rules` 匹配
- `offcraft/install/`：安装计划、库与资源下载、版本落盘
- `offcraft/pack/`：离线包导出/导入

## 开发

```bash
uv sync --frozen
uv run python -m unittest discover -s tests -v
```

依赖方向：本分支不导入 `offcraft.ui` 与 `offcraft.launch`；合并方向 `user-downloader → user`。
