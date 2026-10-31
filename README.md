# CrMCDownload 服务端（server 分支）

Minecraft 文件镜像服务：为客户端代理 Mojang / Fabric / Modrinth 元数据与文件，带磁盘缓存、路径白名单与访问令牌。

## 分支模型

| 分支 | 内容 |
| --- | --- |
| `main` | 文档、架构、部署说明 |
| `user` / `user-*` | 客户端（PyQt6） |
| `server` | 服务端集成（本分支） |
| `server-mirror` | 镜像核心：路由、缓存、代理 |
| `server-deploy` | systemd / Docker / Caddy / nginx 部署物 |

## 环境要求

- Python 3.12
- [uv](https://docs.astral.sh/uv/) 0.12+
- Linux（生产）/ Windows（开发）均可

## 启动

```bash
uv sync --frozen
uv run --frozen main.py
curl http://127.0.0.1:8081/health
```

也可以 `uv run --frozen python -m server_mirror`，两者等价。

## 路由

| 前缀 | 上游 | 说明 |
| --- | --- | --- |
| `/meta/` | `piston-meta.mojang.com`、`launchermeta.mojang.com` | 版本清单 / 版本 JSON |
| `/data/` | `piston-data.mojang.com`、`launcher.mojang.com` | client jar 等 |
| `/libs/` | `libraries.minecraft.net` | 库文件 |
| `/assets/` | `resources.download.minecraft.net` | 资源对象 |
| `/fabric/` | `meta.fabricmc.net` | Fabric loader 元数据 |
| `/fabricmaven/` | `maven.fabricmc.net` | Fabric/Intermediary jar |
| `/forgemaven/` | `maven.minecraftforge.net` | Forge 版本索引与安装器依赖 |
| `/neoforgemaven/` | `maven.neoforged.net` | NeoForge 版本索引与 jar |
| `/modrinth/` | `api.modrinth.com` | 搜索 / 项目 / 版本（**保留查询串**） |
| `/modcdn/` | `cdn.modrinth.com` | `.jar` / `.mrpack` / 图标 |

另有 `/health`、`/metrics`，以及 0.6.0 新增的、**由应用自身提供**（不走上游代理、不做 URL 改写）的端点：

| 端点 | 说明 |
| --- | --- |
| `/api/servers/list\|detail\|status` | 联机热度榜与状态（读接口免令牌） |
| `/api/servers/report`（POST） | 客户端上报新服务器，需 `X-OffCraft-Token`，按 host 限流 |
| `/api/stats/overview\|cache\|download\|servers` | Web 看板数据 |
| `/api/skins/status` | 已存皮肤数与最近更新 |
| `/skin/<name>.png`（GET/POST） | 皮肤托管（Pillow 校验；POST 需令牌） |
| `/page`、`/page/<asset>` | Web 看板静态产物（`MIRROR_PAGE_DIR`，缺 `dist` 时 404） |

- 查询串上限 2048 字节，拒绝反斜杠、NUL 等非法字符；
- 带查询串的请求缓存键为 `<path>.q<sha1(查询串)[:16]>`，避免不同搜索命中同一缓存；
- `meta` / `fabric` / `modrinth` 前缀按 `MIRROR_METADATA_TTL` 过期；maven 路由下 `*.xml`
  （`maven-metadata.xml`）也算元数据，其余 jar 长期缓存；
- 未识别前缀 404，绝不回落到直连。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MIRROR_LISTEN` | `127.0.0.1:8081` | 监听地址 |
| `MIRROR_CACHE_DIR` | `./cache` | 磁盘缓存目录 |
| `MIRROR_TOKEN` | 空 | 非空时校验 `X-OffCraft-Token` |
| `MIRROR_METADATA_TTL` | `600` | 元数据 / Modrinth 响应缓存秒数 |
| `MIRROR_MAX_CACHE_BYTES` | `0` | 缓存上限，0 = 不限 |
| `MIRROR_DB_PATH` | `./data/mirror.db` | 联机 SQLite 路径 |
| `MIRROR_POPULAR_DATA` | `./data/popular_servers.json` | 榜单为空时导入的种子 |
| `MIRROR_SKINS_DIR` | `./skins` | 皮肤文件根目录 |
| `MIRROR_PAGE_DIR` | `./page/dist` | Web 看板构建产物目录 |
| `MIRROR_SERVER_PROBE_INTERVAL` | `300` | 后台探测间隔（秒） |
| `MIRROR_SERVER_PROBE_ENABLE` | `on` | 关闭则不启动探测线程 |
| `UPSTREAM_META` / `_DATA` / `_LIBS` / `_ASSETS` / `_FABRIC` / `_FABRICMAVEN` / `_FORGEMAVEN` / `_NEOFORGEMAVEN` / `_MODRINTH` / `_MODCDN` | 官方地址 | 上游，可替换 |
| `UPSTREAM_PROXY` | 空 | 上游代理（如 `socks5h://127.0.0.1:1080`） |

## 测试

```bash
uv run --group dev python -m pytest tests -q        # 90 项（含 0.6.0 的 db/api/skins/static/probe 测试）
uv run python -m unittest discover -s tests          # 既有 unittest 用例仍可直接跑
```

服务端新依赖（`pyproject.toml`）：运行期 `pillow`（皮肤校验），开发组 `pytest`；探测用标准库 `asyncio`，未引入 aiohttp。

## 部署

见 `server-deploy` 分支的 `DEPLOY.md`。

## 许可证

MIT，见 `LICENSE`。
