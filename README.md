# OffCraft 客户端（CrMcDownload · user 分支）

Minecraft 启动器客户端：从自建镜像下载版本与资源，使用离线账户启动，支持离线包导出/导入。

## 分支模型

| 分支 | 内容 |
| --- | --- |
| `main` | 文档、架构、部署说明 |
| `user` | 客户端集成（本分支） |
| `user-downloader` | 镜像重写、代理、下载器、元数据、安装、离线包 |
| `user-launcher` | 版本解析、rules、classpath、natives、Java、参数、进程 |
| `user-ui` | tkinter 界面 |
| `server` / `server-mirror` / `server-deploy` | 镜像服务端与部署 |

每个分支对应一个独立 clone 目录，组件分支只修改互不重叠的路径，合并方向 `user-* → user`。

## 环境要求

- Windows 10/11 x64
- Python 3.12（`uv` 使用系统解释器，不额外下载）
- [uv](https://docs.astral.sh/uv/) 0.12+

## 快速开始

```bash
uv sync --frozen
uv run python main.py
```

常用命令：

```bash
uv run python -m unittest discover -s tests -v   # 单元测试
uv run pyinstaller OffCraft.spec --noconfirm      # 打包（需系统已装 PyInstaller）
```

## 配置

默认 `%APPDATA%\OffCraft\config.json`，可用 `--data-dir` 或环境变量 `OFFCRAFT_DATA` 覆盖。

```json
{
  "download_base": "https://mc.example.com",
  "download_token": "",
  "proxy": "",
  "max_workers": 8,
  "username": "Player",
  "memory_mb": 4096,
  "game_dir": "C:\\Users\\you\\AppData\\Roaming\\.minecraft"
}
```

## 截图

待补充（M4 完成后）。

## 许可证

MIT，见 `LICENSE`。
