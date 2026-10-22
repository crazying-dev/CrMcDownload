# CrMCDownload 客户端（user 分支）

Minecraft 启动器客户端：从自建镜像下载版本与资源，离线账户启动，PyQt6 界面。

![启动页](../main/docs/images/launch.png)

## 界面

- 顶部导航：启动 / 下载 / 插件 / 设置
- 启动页：左侧分类与版本列表，底部「启动游戏」；右侧显示该版本的 Java 路径、版本路径、Mod 与存档
- 下载页：正式版 / 快照 / 远古版 / 已安装
- 插件页：市场 / 官方 / 社区 / 其他 / 已下载
- **F12**：打开下载源设置（默认 `https://mc.yjlt.top`）

窗口宽高比 3:2，左右栏宽度比 2:3。

## 环境要求

- Windows 10/11 x64
- Python 3.12
- PyQt6（`pip install PyQt6`；本机已安装）
- [uv](https://docs.astral.sh/uv/) 0.12+（可选，用于环境与测试）

## 运行

```bash
python main.py
```

首次启动会使用默认下载源 `https://mc.yjlt.top`，按 **F12** 可修改下载源、令牌、代理与并发数。

## 测试与打包

```bash
uv run --frozen python -m unittest discover -s tests -v
pyinstaller CrMCDownload.spec --noconfirm --clean
# 产物：dist/CrMCDownload.exe
```

## 配置

默认 `%APPDATA%\CrMCDownload\config.json`，可用 `--data-dir` 或 `OFFCRAFT_DATA` 覆盖。
下载源、令牌、代理、并发数不在设置页修改，统一由 F12 窗口维护。

## 分支

`user-downloader`（下载/安装）、`user-launcher`（启动）、`user-ui`（界面）合并到本分支；
服务端见 `server` / `server-mirror` / `server-deploy`。

## 许可证

MIT。
