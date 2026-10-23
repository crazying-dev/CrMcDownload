# CrMCDownload 客户端（user 分支）

Minecraft 启动器客户端：从自建镜像下载游戏版本、模组、整合包、资源包与光影，离线账户启动，PyQt6 界面（PCL 风格）。

## 界面

顶部固定导航栏（68 px，启动 / 下载 / 设置 / 插件）+ 下方内容区；窗口长宽比 3:2（默认 1200×800，最小 1080×720）：

- **启动游戏**：账号行（头像 / 用户名 / 切换账号）、版本大图上位卡（加载器与 MC 版本徽章、版本元信息）、
  内存分配滑块（1–32 GB）、「启动游戏」主按钮、版本设置 / 打开目录；右侧版本列表
  （版本 / 加载器 / 大小 / 最近使用；右键：启动、版本设置、**安装加载器…**、打开游戏目录、打开版本目录、删除实例）
  与 6 个快捷入口磁贴。
- **下载中心**：游戏版本（正式版 / 快照 / 远古版 / 已安装 四个分段，支持搜索与「安装最新版」）、
  模组、整合包、资源包、光影（Modrinth 搜索 → 详情 → 版本 → 一键安装）、**Java**
  （Mojang 官方 java-runtime 列表，可下载并设为启动 Java）。
- **插件**：市场 / 官方 / 社区 / 其他 / 已下载 五个分类的框架页，插件 API 尚未接入。
- **设置**：游戏目录、版本隔离、跳过资源校验、Java 选择（自动检测 / 手动添加）、内存、额外 JVM 参数、
  下载源（**只读展示**，按 F12 或点「修改下载源（F12）」才能改）、关于与数据目录入口。
- 底部任务面板（标题 / 阶段 / 详情 / 进度条 / 取消）与状态栏（连接状态灯、下载源、实例与文件计数）。

下载源与运行日志：下载源默认由发布者固定，正常界面无法修改，按 **F12** 打开修改窗口；
启动器不提供任何日志查看入口（日志仅写入数据目录，供排查用）。

### 下载源择优（镜像 vs 直连）

`offcraft/net/latency.py` 的 `SourceRouter` 代替原始 `Mirror` 作为下载源：同一个上游文件
先分别量一次「镜像中转」和「上游直连」的首字节时间（新建连接 + `Range: bytes=0-2047`，
2 个样本取最快，单次 5s 超时），谁快用谁，决策按路由缓存 600s。

- 设置页开关「下载时比较镜像与直连的延迟，自动用更快的一边」（`config.json` 的 `prefer_fastest`，默认开）；
- 关闭或 `--no-fastest` 时回到恒走镜像的旧行为（`Mirror`）；
- 镜像无对应路由 / 双方都不可达 / 只剩一方可达都有明确分支，结论记录在 `Decision.detail`；
- 设置页「测试连接」会清空缓存后重新探测，并追加每个路由的时延结论；
- Modrinth 多个候选域名用 `SourceRouter.rank()` 按实测时延排序后取第一个。

## 加载器与 Java

| 加载器 | 安装方式 | 能否装模组 / 光影 |
| --- | --- | --- |
| 原版 `vanilla` | 只装 Mojang 文件 | 否（只能装资源包） |
| Fabric | 拉 loader profile JSON 并补齐依赖库 | 是 |
| Forge | 下载 installer jar，`--installClient` 安装 | 是 |
| NeoForge | 同上 | 是 |

- 安装版本时先弹「选择加载器」，默认原版；已有实例可在版本右键菜单「安装加载器…」补装。
- 模组 / 光影只能在带加载器的实例上安装：内容页会把原版实例标为「原版·不支持模组」并禁用安装按钮。
- Java 选项卡读取 `launchermeta` 的 `java-runtime/all.json`，展开到 `runtime/<component>/bin/java.exe`，
  与启动器自动选 Java 的扫描路径一致；Forge / NeoForge 安装器必须有可用 Java。

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
- **限制**：Forge / NeoForge 整合包需要先有对应加载器实例，安装器不会由整合包自动触发。

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

powershell -NoProfile -ExecutionPolicy Bypass -File packaging\build_installer.ps1   # 再打安装包
# 产物：dist/installer/CrMCDownload-<版本>-win64-setup.exe
```

安装包口径（自 0.5.3 起）：

- 安装器是**自包含的 C# 程序**，源码 [`packaging/installer/Setup.cs`](packaging/installer/Setup.cs)，
  用 Windows 自带的 .NET Framework 编译器 `csc.exe` 编译，不需要 Inno Setup / NSIS / WiX；
- 应用负载以 zip 资源（`payload.zip`）内嵌在安装器中，**直接解包到目标目录**，
  全程不往 `%TEMP%` 写任何东西（规避「安装器无法创建 %TEMP%\is-XXXX.tmp，Error 5」一类环境问题）；
- 发布者统一为 **Cr**（`AssemblyCompany` / 回收站「应用和功能」`Publisher`）；
- 安装包本体带四段式文件信息（`FileVersion` / `ProductVersion` / `ProductName` / `CompanyName` / `Copyright`）；
- 覆盖升级：默认目录固定 `%LOCALAPPDATA%\Programs\CrMCDownload`，重跑新版安装包直接覆盖即可，
  无需先卸载；旧版正在运行时会先尝试关窗口，关不掉则提示用户手动退出；
- 卸载：安装目录内 `uninstall.exe`（与安装器同一份源码编译，约 33 KB，等价于 `setup.exe /UNINSTALL`），
  同时登记 `HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\CrMCDownload`，
  在「应用和功能」里可见（发布者 Cr、版本号、图标、卸载命令）；
- 受限目录自检：若安装包从**受限目录**（例如 `E:\root\WorkSpaces\` 下的项目工作区）启动，
  Windows 会拒绝该进程写入 C 盘，安装器会在窗口出现时/静默运行时立即给出中文说明
  （`无法写入以下位置…请把安装包复制到「下载」文件夹或桌面`），并把日志落到安装包同目录；
  把安装包复制到下载文件夹再双击即可正常安装；
- 静默安装：`setup.exe /S [/DIR=<目录>] [/NODESKTOPICON] [/NOLAUNCH]`，卸载：`setup.exe /UNINSTALL /S`。

> 注意：布局/临时目录相关的环境故障会让**任何**安装器失败，排查时可先用
> `setup.exe /S /DIR=<某个目录>` 快速区分「安装器问题」与「系统权限问题」。

## 配置

默认 `%APPDATA%\CrMCDownload\config.json`，可用 `--data-dir` 或 `OFFCRAFT_DATA` 覆盖。

## 分支

`user-downloader`（下载/安装/内容层）、`user-launcher`（启动）、`user-ui`（界面）合并到本分支；
服务端见 `server` / `server-mirror` / `server-deploy`。

## 许可证

MIT。
