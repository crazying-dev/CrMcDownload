# 部署指南

目标：在 `yjlt.top`（Ubuntu 24.04）上运行镜像服务，由 nginx 反向代理到 `https://mc.yjlt.top`。

> 现有 80/443 已被主站占用，因此使用**子域名**最干净；如果暂时没有 DNS 记录，也可以先监听独立端口（见文末）。

## 0. 前置条件

- 子域名 `mc.yjlt.top` 的 A 记录指向 `154.219.110.63`
- 服务器可直连 Mojang（本项目已在 `yjlt.top` 实测 200）
- 磁盘 ≥ 20 GB（全量缓存会随使用增长）

## 1. 拉取代码

```bash
cd ~
git clone -b server https://github.com/crazying-dev/CrMcDownload.git CrMCDownload
cd CrMCDownload
# 安装 uv（若未安装）
curl -LsSf https://astral.sh/uv/install.sh | sh
~/.local/bin/uv sync --frozen
~/.local/bin/uv run --frozen main.py   # 前台验证，Ctrl+C 退出
```

## 2. 配置

复制示例并修改：

```bash
sudo mkdir -p /var/lib/offcraft-mirror
sudo chown mimi:mimi /var/lib/offcraft-mirror
cp .env.example .env
# 编辑 .env：设置 MIRROR_TOKEN、MIRROR_CACHE_DIR=/var/lib/offcraft-mirror
```

生成令牌：

```bash
openssl rand -hex 24
```

## 3. systemd

```bash
sudo cp systemd/offcraft-mirror.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now offcraft-mirror
curl -s http://127.0.0.1:8081/health
```

## 4. nginx + 证书

```bash
sudo cp nginx/offcraft.conf /etc/nginx/sites-available/offcraft.conf
sudo ln -s /etc/nginx/sites-available/offcraft.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d mc.yjlt.top
```

验证：

```bash
curl -I https://mc.yjlt.top/meta/mc/game/version_manifest_v2.json
```

## 5. 客户端

客户端配置：

```json
{
  "download_base": "https://mc.yjlt.top",
  "download_token": "<与 MIRROR_TOKEN 相同>"
}
```

或命令行：

```bash
CrMCDownload.exe --base https://mc.yjlt.top --token <token> install 1.14
```

## 5.1 内容路由（模组 / 整合包 / 资源包 / 光影 / 加载器）

镜像支持以下新前缀，客户端内容下载优先走它们，镜像不可用时才回落到官方源：

| 前缀 | 上游 | 备注 |
| --- | --- | --- |
| `/fabricmaven/` | `maven.fabricmc.net` | Fabric loader 的 jar 依赖 |
| `/forgemaven/` | `maven.minecraftforge.net` | Forge 版本索引（`maven-metadata.xml`）与安装器依赖 |
| `/neoforgemaven/` | `maven.neoforged.net` | NeoForge 版本索引与 jar |
| `/modrinth/` | `api.modrinth.com` | 搜索 / 项目 / 版本，保留查询串 |
| `/modcdn/` | `cdn.modrinth.com` | `.jar` / `.mrpack` / 图标 |

Forge / NeoForge 的**安装器自身**会直连各自 maven 拉依赖，这部分不经过镜像（客户端只能保证版本索引走镜像）。

部署后自检：

```bash
curl -s -o /dev/null -w '%{http_code}\n' "https://mc.yjlt.top/modrinth/v2/search?query=sodium&limit=1"   # 200
curl -s -o /dev/null -w '%{http_code}\n' "https://mc.yjlt.top/modrinth/v2/search?query=sodium&limit=1"   # 二次请求应为 200 且 /metrics 上游计数不变
curl -s -o /dev/null -w '%{http_code}\n' "https://mc.yjlt.top/forgemaven/net/minecraftforge/forge/maven-metadata.xml"  # 200
```

如需改写上游（例如换镜像加速），在 `.env` 里补 `UPSTREAM_FABRICMAVEN` / `UPSTREAM_FORGEMAVEN` / `UPSTREAM_NEOFORGEMAVEN` / `UPSTREAM_MODRINTH` / `UPSTREAM_MODCDN`。

## 备选：无子域名（独立端口）

若暂时不能加 DNS，可让 Caddy 监听 `:8443` 并自签/使用已有证书：

```bash
caddy run --config caddy/Caddyfile --adapter caddyfile
```

客户端把 `download_base` 设为 `https://yjlt.top:8443`，并确保防火墙放行 8443。

## 运维

| 操作 | 命令 |
| --- | --- |
| 查看状态 | `systemctl status offcraft-mirror` |
| 查看日志 | `journalctl -u offcraft-mirror -f` |
| 缓存命中 | `curl -s http://127.0.0.1:8081/metrics` |
| 清理缓存 | `sudo rm -rf /var/lib/offcraft-mirror/*`（服务无需重启） |
| 升级 | `git pull && uv sync --frozen && sudo systemctl restart offcraft-mirror` |

## 安全

- 必须设置 `MIRROR_TOKEN`，避免成为公开下载源；
- nginx 层可再加 IP 白名单或 basic auth；
- 不要把 `.env` 提交到仓库。
