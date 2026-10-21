# server-mirror（CrMcDownload 服务端·镜像核心）

- `server_mirror/config.py`：环境变量与默认值
- `server_mirror/routes.py`：前缀路由与路径白名单
- `server_mirror/cache.py`：磁盘缓存（原子写、TTL）
- `server_mirror/proxy.py`：HTTP 服务、Range 透传、metrics
- `server_mirror/__main__.py`：`python -m server_mirror`

## 开发

```bash
uv sync --frozen
uv run python -m unittest discover -s tests -v
```

合并方向 `server-mirror → server`。
