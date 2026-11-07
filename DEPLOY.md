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

### 容量与并发限制（建议生产环境显式配置）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MIRROR_MAX_CACHE_BYTES` | `0`（不限） | 缓存总量上限；超出后按最久未访问（mtime）淘汰。**生产环境必须设置**，例如 `214748364800`（200 GiB） |
| `MIRROR_MAX_FILE_BYTES` | `1073741824`（1 GiB） | 单个文件入缓存上限；超过的文件仍会正常转发，只是不落盘 |
| `MIRROR_MAX_UPSTREAM` | `8` | 同时进行的上游回源请求上限，超额请求等待 `MIRROR_UPSTREAM_WAIT` 秒后返回 503 |
| `MIRROR_UPSTREAM_WAIT` | `30` | 等待回源名额的秒数 |
| `MIRROR_FLIGHT_WAIT` | `60` | 同一文件已被别的请求下载时，最多等多久再直接读缓存 |
| `MIRROR_METADATA_TTL` | `600` | 元数据（版本清单、maven `*.xml`、Modrinth API）缓存秒数 |
| `MIRROR_SWEEP_INTERVAL` | `300` | 过期缓存回收的最小间隔（秒） |

几个行为约定：

- 同一个文件被多个客户端同时请求时只会回源一次（single-flight），其余请求等它落盘后直接读缓存；
- 过期元数据与残留临时文件由服务端定期清理，不会无限堆积；
- 磁盘写满时自动清临时文件并压缩缓存到上限的一半，不影响正在传输的响应；
- `/health` 会返回缓存用量（`cache.bytes` / `cache.max_bytes` / 淘汰与回收累计值）。

## 6. 联机 / 皮肤 / Web 看板（0.6.0）

镜像服务除代理上游文件外，还**自身提供**三类端点（不走上游代理、无需额外 nginx 规则，`/api` `/skin` `/page`
经同一 `server {}` 反代即可）：

| 端点 | 说明 |
| --- | --- |
| `/api/servers/*`、`/api/stats/*`、`/api/skins/status` | 联机榜单与看板数据（读免令牌） |
| `/api/servers/report`（POST）、`/skin/<name>.png`（POST） | 写接口，需 `X-OffCraft-Token` |
| `/page`、`/page/<asset>` | Web 可视化看板静态产物（Vue3 构建的 `dist`） |

### 6.1 构建 Web 看板

看板需在**有 node + pnpm 的机器**（本机或 CI）构建，产物目录 `server/page/dist` 由 `MIRROR_PAGE_DIR` 指向：

```bash
cd page
pnpm install
pnpm build          # 产出 page/dist（base=/page/）
```

未构建时 `/page` 返回 404，不影响其它路由（看板是可选功能）。开发态可用 `pnpm dev`（`vite.config.ts` 已把
`/api`、`/health` 代理到 `127.0.0.1:8081`）。

### 6.2 新增环境变量与持久化

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MIRROR_DB_PATH` | `./data/mirror.db` | 联机 SQLite（WAL，会产生 `-wal`/`-shm` 旁文件） |
| `MIRROR_POPULAR_DATA` | `./data/popular_servers.json` | 榜单为空时导入的种子（人工维护） |
| `MIRROR_SKINS_DIR` | `./skins` | 皮肤文件根目录 |
| `MIRROR_PAGE_DIR` | `./page/dist` | 看板构建产物目录 |
| `MIRROR_SERVER_PROBE_INTERVAL` | `300` | 后台批量探测间隔（秒） |
| `MIRROR_SERVER_PROBE_ENABLE` | `on` | 关闭则不启动探测线程 |

- **持久化与备份**：systemd 把工作目录设在 `/var/lib/offcraft-mirror` 时，`.env` 里把上述路径改为该目录下的绝对路径
  （如 `MIRROR_DB_PATH=/var/lib/offcraft-mirror/data/mirror.db`、`MIRROR_SKINS_DIR=/var/lib/offcraft-mirror/skins`）；
  Docker 部署需为 `./data` 与 `./skins` 挂载持久卷，否则容器重建会丢失热度榜与已上传皮肤。
- **回滚 / 停止**：探测线程跑在独立事件循环里，`systemctl stop` / `Ctrl-C` 会 `loop.close()` + 线程 join 后干净退出；
- 皮肤上传上限 64 KiB、尺寸仅 `64×64` / `64×32`（Pillow 校验），非 PNG 或尺寸不符返回中文错误。

### 6.3 离线联机：特权插件按需下载与中继

离线联机以官方特权插件 `offline-multiplayer` 形态提供（基础安装包不内置、客户端在插件页按需下载启用）。
服务端参与两部分：

- **插件包静态下发**：`GET /plugin/offline-multiplayer.manifest.json` 与 `GET /plugin/offline-multiplayer.zip`（**免令牌、
  只读、防路径穿越**），与 `/api` `/skin` `/page` 走**同一 `server {}` 反代**，无需新增 nginx 规则。文件由
  `devtools/build_mp_plugin.py` 产出（zip + `sha256/size/abi` 描述符），落到 `MIRROR_PLUGIN_DIR` 指向的目录：

  ```bash
  # 在有 pynacl 的构建机（与客户端同 ABI：cp312-win_amd64）上产出，拷入插件目录
  cd user   # 或仓库根；脚本以源码树 offcraft/p2p 为输入
  python ../devtools/build_mp_plugin.py --out "$MIRROR_PLUGIN_DIR"   # 默认 server/plugins
  ```

  部署后自检：

  ```bash
  curl -s -o /dev/null -w '%{http_code}\n' "https://mc.yjlt.top/plugin/offline-multiplayer.manifest.json"  # 200
  curl -s -o /dev/null -w '%{http_code}\n' "https://mc.yjlt.top/plugin/offline-multiplayer.zip"            # 200
  ```

- **中继信令与转发**：控制面走主 HTTP 端口的 `POST /p2p/*`（经同一反代）；数据面由服务端的独立 **TCP 中继**
  转发端到端密文帧。**中继端口不变**（默认 `25577`），需在防火墙/安全组单独放行 TCP（非 80/443）；
  服务端只见密文、**不解密、不持明文/密码/auth_key**，准入靠客户端 `proof` HMAC，房间登记存于 `p2p_rooms`（仅名字/属主/时间）。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MIRROR_P2P_ENABLE` | `off` | 开启中继信令路由（未设则 `/p2p/*` 与中继不启用） |
| `MIRROR_P2P_RELAY_PORT` | `25577` | 数据面 TCP 中继监听端口（**与一期一致，不变**） |
| `MIRROR_P2P_RELAY_HOST` | 空（回落 `host`） | 对外通告地址，供客户端连回中继（反代/NAT 后需设为公网可达地址） |
| `MIRROR_P2P_MAX_ROOMS` | `256` | 同时在途实时会话上限（内存态，防滥用） |
| `MIRROR_P2P_MAX_ROOMS_PER_DEVICE` | `10` | 每设备（`owner=device_id`）未删除房间配额上限 |
| `MIRROR_P2P_ROOM_TTL` | `180` | 实时会话无活动回收秒数 |
| `MIRROR_P2P_RELAY_BYTES_CAP` | `536870912` | 单条中继连接累计转发字节上限（超限断开，避免开放中继） |
| `MIRROR_PLUGIN_DIR` | `./plugins` | `GET /plugin/*` 读取的插件包目录（systemd/Docker 下建议绝对路径并挂载） |

- **房主控制面（`/p2p/roster` `/p2p/kick`）**：`开放` 时服务端随机生成 `host_secret`、仅回给房主（不进名册）；
  `kick` 服务端校验 `host_secret`+`host_sid` 后经 `RelayServer.evict()` 即时拆除该成员的中继桥并撤销在途令牌，
  将 `sid` 加入 `room.blocked` 阻断其旧会话重连。踢出为「断开当前会话」、非永久封禁（被踢者持密码可重新 `加入`）。
  控制面仍走同一 `POST /p2p/*`，**无新增端口/路由**。

- **虚拟局域网（UDP 打洞 + 中继）**：开启后服务端额外启动 **UDP 边缘服务**（默认 `25578/udp`）。
  vpnd 首包 `HELLO <edge_token>` 登记公网端点，双方每 500ms 发 PUNCH 打洞，6s 不通自动改为经边缘
  `R <sid>` 转发密文；边缘与 TCP 中继一样**只见密文**。令牌绑定 房间+设备、TTL 180s，
  长会话由 `/p2p/roster` 轮询自动续签。需在防火墙/安全组放行 **UDP 25578**；
  TCP 中继 `25577` 保留，作为回滚路径。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MIRROR_P2P_VPN_ENABLE` | `on` | 虚拟局域网总开关；设 `off` 后不下发 `vip/edge_token`，客户端自动回落 TCP 隧道 |
| `MIRROR_P2P_UDP_PORT` | `25578` | UDP 边缘监听端口（需单独放行 UDP） |
| `MIRROR_P2P_UDP_BYTES_CAP` | `536870912` | 单会话 UDP 中继累计转发字节上限 |
| `MIRROR_P2P_EDGE_TOKEN_TTL` | `180` | 边缘令牌有效期（秒），到期由名册轮询重新签发 |

> **客户端前提**：虚拟局域网需要基础包内的 `vpnd.exe`（Wintun 数据面）。首次启用会弹出一次 UAC 安装；
> 拒绝授权或驱动被安全软件拦截时，面板会提示并自动回落旧 TCP 隧道，不影响联机。
> 卸载时主卸载程序会调用 `vpn-setup.exe /uninstall`，清理服务、虚拟网卡与防火墙规则。

> **生效方式（三层产物联动）**：改服务端信令/中继代码（`server_mirror/p2p.py`，含 `kick`/`roster`/`evict`/`host_secret`）
> 需 `git pull` 到部署目录后**重启进程**（`bash run.sh`，`.env` 经 `set -a` 注入）方生效；改动 `server/plugins/` 插件包
> 或 `page/dist` 前端产物属**文件层**，替换即生效、**不需重启**；客户端引擎/UI 变更则随插件重下载/安装包发版。

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
| 缓存用量 | `curl -s http://127.0.0.1:8081/health`（看 `cache` 字段） |
| 清理缓存 | `sudo rm -rf /var/lib/offcraft-mirror/*`（服务无需重启） |
| 升级 | `git pull && uv sync --frozen && sudo systemctl restart offcraft-mirror` |

## 安全

- 必须设置 `MIRROR_TOKEN`，避免成为公开下载源；
- nginx 层可再加 IP 白名单或 basic auth；
- 不要把 `.env` 提交到仓库。
