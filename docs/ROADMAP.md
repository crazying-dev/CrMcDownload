# 路线图

| 里程碑 | 内容 | 状态 |
| --- | --- | --- |
| M0 | 仓库与 8 个分支、uv 工程骨架、文档 | 已完成 |
| M1 | 下载核心：镜像重写、代理、下载器、元数据、安装器 + 单测 | 已完成 |
| M2 | 启动核心：classpath、natives、Java、参数、进程；真实安装 1.14 | 已完成 |
| M3 | 镜像服务端 + 部署物；`mc.yjlt.top` 上线 | 已完成 |
| M4 | PyQt6 界面：启动页、下载中心（游戏/模组/整合包/资源包/光影）、设置、日志 | 已完成 |
| M5 | 离线能力：断网启动、离线包导出/导入、Java 本地化 | 已完成 |
| M6 | 部署到生产服务器并切换域名实测 | 已完成 |
| M7 | 内容生态：Modrinth 搜索/安装、`fabricmaven`/`modrinth`/`modcdn` 路由、整合包安装 | 已完成 |
| M8 | Forge / NeoForge / Quilt 整合包自动安装加载器 | 待开始 |
| M9 | PyInstaller 打包、CI、发布 | 进行中 |

## 验收基线

1. 空目录经镜像安装 1.14，全部文件 SHA-1 校验通过；
2. 安装 26.1.2（Java 25）并启动到主菜单；
3. 断网后已安装版本仍可启动，启动阶段零网络请求；
4. 离线包导入后可无网启动；
5. 客户端请求只发往 `download_base`（内容层在镜像不可用时才回落官方 Modrinth）；
6. 镜像请求命中缓存，`/metrics` 上游计数为 0；
7. `CrMCDownload.exe` 免 Python 完成安装与启动；
8. 模组/资源包/光影一键安装到当前实例，整合包自动建实例并可启动。

## 当前测试基线

| 仓库 | 命令 | 数量 |
| --- | --- | --- |
| `user` | `python -m unittest discover -s tests` | 72 |
| `user-downloader` | 同上 | 59 |
| `user-launcher` | 同上 | 50 |
| `server-mirror` / `server` | 同上 | 14 |

另有离屏界面自检 `devtools/ui_smoke.py`（断言式）与截图 `devtools/ui_shots.py`。
