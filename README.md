# CrMCDownload

自建镜像 + 离线账户的 Minecraft 启动器。客户端（PyQt6，PCL 风格）从你的镜像服务器下载原版版本、模组、整合包、资源包与光影，
服务端代理 Mojang / Fabric / Modrinth 文件并做磁盘缓存。

> 背景：2026 年 10 月起，PCL、HMCL 等第三方启动器陆续停止提供游戏下载。本项目把“下载源”做成可自建的镜像服务，
> 客户端默认只与你的域名通信。

## 组成

```
┌──────────────── 客户端 (user, PyQt6) ────────────┐   ┌──────────── 服务端 (server) ────────────┐
│ 启动 · 下载中心 · 设置 · 实例 · 离线包 · 日志     │──▶│ 镜像代理 + 磁盘缓存 + 令牌 + metrics      │──▶ Mojang
│ Modrinth 内容层（模组/整合包/资源包/光影）        │   │ /meta /data /libs /assets /fabric         │──▶ Fabric
└────────────────────────────────────────────┘HTTPS└─ /fabricmaven /modrinth /modcdn ────────┘──▶ Modrinth
```

## 分支

| 分支 | 内容 |
| --- | --- |
| `main` | 文档（本分支） |
| `user` | 客户端集成：入口、配置、实例、内容层、打包、集成测试 |
| `user-downloader` | 镜像重写、代理、下载器、元数据、安装、离线包、内容层（`offcraft/content/`） |
| `user-launcher` | 版本解析、rules、classpath、natives、Java、参数、进程 |
| `user-ui` | PyQt6 界面（主题、控件、内容页、主窗口） |
| `server` | 服务端集成：`main.py` 入口、配置、测试 |
| `server-mirror` | 镜像核心：路由、缓存、Range、metrics |
| `server-deploy` | systemd / Docker / Caddy / nginx 部署物 |

每个分支在本地是一个独立 clone，目录名与分支名一致；合并方向 `user-* → user`、`server-* → server`。
详见 [docs/BRANCHES.md](docs/BRANCHES.md)。

## 客户端

顶部导航栏（启动 / 下载 / 设置 / 插件）+ 下方内容区，窗口固定 3:2（默认 1200×800，最小 1080×720）：

- **启动游戏**：账号行、版本大图卡、内存滑块、启动主按钮；右侧版本列表（右键：启动 / 版本设置 /
  **安装加载器…** / 打开游戏目录 / 删除实例）与 6 个快捷磁贴
- **下载中心**：游戏版本（正式版 / 快照 / 远古版 / 已安装，支持搜索与安装最新版）、
  模组 / 整合包 / 资源包 / 光影（Modrinth 搜索 → 详情 → 版本 → 一键安装）、Java（Mojang 官方 java-runtime）
- **插件**：市场 / 官方 / 社区 / 其他 / 已下载（框架，插件 API 未接入）
- **设置**：游戏目录、版本隔离、Java（自动检测 / 手动添加）、内存、JVM 参数、
  下载源（只读，按 F12 才能改），不再提供日志查看入口
- 底部任务面板与状态栏（连接状态、下载源、实例统计）

装版本时可选择加载器（原版 / Fabric / Forge / NeoForge）：原版只能装资源包，模组与光影只能装到带加载器的实例。
内容下载默认走镜像（`/modrinth`、`/modcdn`），镜像不可用时自动回落到 Modrinth 官方源，徽章显示当前通道。

```bash
cd user
python main.py                     # 需要 PyQt6
# 或打包
pyinstaller CrMCDownload.spec --noconfirm --clean   # dist/CrMCDownload/CrMCDownload.exe
```

配置默认在 `%APPDATA%\CrMCDownload\config.json`。

## 服务端

```bash
cd server
uv sync --frozen
uv run --frozen main.py
curl http://127.0.0.1:8081/health
```

路由：`/meta` `/data` `/libs` `/assets` `/fabric` `/fabricmaven` `/forgemaven` `/neoforgemaven`
`/modrinth` `/modcdn`，另带 `/health` 与 `/metrics`。带查询串的请求（如 Modrinth 搜索）会完整转发并以 `.q<sha1>` 区分缓存；
maven 路由下的 `*.xml` 索引按元数据 TTL 缓存，jar 长期缓存。

生产部署见 `server-deploy` 分支的 `DEPLOY.md`（systemd / Docker / nginx / Caddy），
当前线上：`https://mc.yjlt.top`。

## 技术栈

- Python 3.12 + uv
- 客户端 GUI：PyQt6（唯一第三方运行期依赖）
- 服务端：标准库 `ThreadingHTTPServer`，可选 nginx/Caddy 反代
- 打包：PyInstaller（onedir 文件夹分发，免运行期解压）

## 文档

- [架构](docs/ARCHITECTURE.md)
- [分支与协作](docs/BRANCHES.md)
- [路线图](docs/ROADMAP.md)

## 许可证

MIT，见 [LICENSE](LICENSE)。
