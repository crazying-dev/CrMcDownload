# CrMCDownload 服务端（server 分支）

Minecraft 文件镜像服务：为客户端代理 Mojang / Fabric 元数据与文件，带磁盘缓存、路径白名单与访问令牌。

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

| 前缀 | 上游 |
| --- | --- |
| `/meta/` | `piston-meta.mojang.com`、`launchermeta.mojang.com` |
| `/data/` | `piston-data.mojang.com`、`launcher.mojang.com` |
| `/libs/` | `libraries.minecraft.net` |
| `/assets/` | `resources.download.minecraft.net` |
| `/fabric/` | `meta.fabricmc.net` |

另有 `/health`、`/metrics`。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MIRROR_LISTEN` | `127.0.0.1:8081` | 监听地址 |
| `MIRROR_CACHE_DIR` | `./cache` | 磁盘缓存目录 |
| `MIRROR_TOKEN` | 空 | 非空时校验 `X-OffCraft-Token` |
| `UPSTREAM_META` / `UPSTREAM_DATA` / `UPSTREAM_LIBS` / `UPSTREAM_ASSETS` / `UPSTREAM_FABRIC` | 官方地址 | 上游，可替换 |
| `UPSTREAM_PROXY` | 空 | 上游代理（如 `socks5h://127.0.0.1:1080`） |

## 部署

见 `server-deploy` 分支的 `DEPLOY.md`。

## 许可证

MIT，见 `LICENSE`。
