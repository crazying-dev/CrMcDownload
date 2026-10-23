# server-mirror（CrMcDownload 服务端·镜像核心）

纯标准库实现的只读镜像代理：路径白名单 + 磁盘缓存 + Range 透传 + metrics。

## 模块

- `server_mirror/config.py`：环境变量与默认上游表
- `server_mirror/routes.py`：前缀路由、路径白名单、查询串规范化与缓存键
- `server_mirror/cache.py`：磁盘缓存（原子写、TTL、元数据/文件分级）
- `server_mirror/proxy.py`：HTTP 服务、Range 透传、metrics
- `server_mirror/__main__.py`：`python -m server_mirror`

## 路由

| 前缀 | 上游 |
| --- | --- |
| `/meta/` | `piston-meta.mojang.com` |
| `/data/` | `piston-data.mojang.com` |
| `/libs/` | `libraries.minecraft.net` |
| `/assets/` | `resources.download.minecraft.net` |
| `/fabric/` | `meta.fabricmc.net` |
| `/fabricmaven/` | `maven.fabricmc.net` |
| `/modrinth/` | `api.modrinth.com` |
| `/modcdn/` | `cdn.modrinth.com` |

- **查询串**：`/modrinth/v2/search?query=...&facets=...` 这类请求会把查询串完整转发到上游（上限 2048 字节，
  拒绝反斜杠、NUL 等非法字符）；缓存键追加 `.q<sha1(查询串)[:16]>`，避免不同查询命中同一缓存文件。
- **缓存分级**：`meta` / `fabric` / `modrinth` 按 `MIRROR_METADATA_TTL` 过期，其余内容文件长期缓存。
- 未识别前缀直接 404，绝不回落到直连。

## 开发

```bash
uv sync --frozen
uv run python -m unittest discover -s tests -v    # 14 项
```

合并方向 `server-mirror → server`。
