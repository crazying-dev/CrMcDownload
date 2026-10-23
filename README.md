# CrMCDownload 客户端（user 分支）

Minecraft 启动器客户端：从自建镜像下载游戏版本、模组、整合包、资源包与光影，离线账户启动，PyQt6 界面（PCL 风格）。

## 界面

左侧固定侧边栏（212 px）+ 右侧内容区：

- **启动游戏**：账号行（头像 / 用户名 / 切换账号）、版本大图上位卡（加载器与 MC 版本徽章、版本元信息）、
  内存分配滑块（1–32 GB）、「启动游戏」主按钮、版本设置 / 打开目录；右侧版本列表
  （版本 / 加载器 / 大小 / 最近使用；右键：启动、版本设置、打开游戏目录、打开版本目录、删除实例）
  与 6 个快捷入口磁贴。
- **下载中心**：游戏版本（正式版 / 快照 / 远古版 / 已安装 四个分段，支持搜索与「安装最新版」）、
  模组、整合包、资源包、光影（Modrinth 搜索 → 详情 → 版本 → 一键安装）。
- **设置**：游戏目录、版本隔离、跳过资源校验、Java 选择（自动检测 / 手动添加）、内存、额外 JVM 参数、
  下载源（地址 / 令牌 / 代理 / 并发 + 测试连接）、关于与数据目录入口。
- 底部任务面板（标题 / 阶段 / 详情 / 进度条 / 取消）与状态栏（连接状态灯、下载源、实例与文件计数）。

## 内容下载

统一由 `offcraft/content/` 提供：

| 类型 | 来源 | 安装位置 |
| --- | --- | --- |
| 模组 `mod` | Modrinth 搜索 / 版本 / 必需依赖 | `<实例游戏目录>/mods` |
| 资源包 `resourcepack` | 同上 | `<实例游戏目录>/resourcepacks` |
| 光影 `shader` | 同上 | `<实例游戏目录>/shaderpacks` |
| 整合包 `modpack` | `.mrpack` | `versions/<实例名>`（含 `overrides/`） |

- **镜像优先**：先请求 `{download_base}/modrinth/...` 与 `/modcdn/...`，镜像不可用或返回非 2xx 时自动回落到
  官方源（`api.modrinth.com` / `cdn.modrinth.com`），详情页徽章实时显示当前通道（镜像 / 官方源）。
- **依赖解析**：安装模组时对 `required` 依赖做广度优先安装（上限 24 个文件，避免异常数据导致无限扩张）。
- **整合包**：解析 `modrinth.index.json`，按 `dependencies` 安装基础版本（Fabric 自动拉取 loader profile），
  再解压 `overrides/`，最后写入 `offcraft-pack.json` 记录来源与文件数。
- **限制**：Forge / NeoForge / Quilt 整合包暂不自动安装加载器（写占位版本 JSON 并给出警告）。

## 版本隔离

`config.json` 的 `isolation` 默认 `true`：每个版本以 `versions/<id>/` 作为游戏目录，
`options.txt`、`saves`、`mods`、`screenshots` 相互隔离；整合包实例恒为隔离，并通过 `offcraft-pack.json` 定位目录。
设置页可关闭，关闭后回到原生布局（共享 `.minecraft`）。

## 环境要求

- Windows 10/11 x64
- Python 3.12
- PyQt6（`pip install PyQt6`）
- [uv](https://docs.astral.sh/uv/) 0.12+（可选，用于环境与测试）

## 运行

```bash
cd user
python main.py            # 打开图形界面
python main.py list       # CLI：list / install / check / instances / launch / pack / gui
```

首次启动使用默认下载源 `https://mc.yjlt.top`，设置页可修改下载源、令牌、代理与并发数。

## 测试与打包

```bash
python -m unittest discover -s tests -v          # 单元测试
python ../devtools/ui_smoke.py                   # 离屏界面自检（断言式）
python ../devtools/ui_shots.py .testbed/shots    # 输出界面截图
pyinstaller CrMCDownload.spec --noconfirm --clean
# 产物：dist/CrMCDownload/CrMCDownload.exe（文件夹分发）
```

## 配置

默认 `%APPDATA%\CrMCDownload\config.json`，可用 `--data-dir` 或 `OFFCRAFT_DATA` 覆盖。

## 分支

`user-downloader`（下载/安装/内容层）、`user-launcher`（启动）、`user-ui`（界面）合并到本分支；
服务端见 `server` / `server-mirror` / `server-deploy`。

## 许可证

MIT。
