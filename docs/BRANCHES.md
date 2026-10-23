# 分支与协作

## 本地布局

每个分支一个独立 clone，目录名 = 分支名：

```
MC/
  main/              # 文档
  user/              # 客户端集成
  user-downloader/   # 下载与安装
  user-launcher/     # 启动
  user-ui/           # 界面
  server/            # 服务端集成
  server-mirror/     # 镜像核心
  server-deploy/     # 部署物
```

## 路径所有权（避免合并冲突）

| 分支 | 只能修改 |
| --- | --- |
| `user` | `main.py`、`offcraft/__init__.py`、`offcraft/config.py`、`offcraft/instances.py`、`offcraft/logbus.py`、`offcraft/cli.py`、`OffCraft.spec`、`build.ps1`、集成测试 |
| `user-downloader` | `offcraft/mirror.py`、`offcraft/net/`、`offcraft/meta/`、`offcraft/install/`、`offcraft/pack/`、`offcraft/content/` |
| `user-launcher` | `offcraft/launch/`、`offcraft/diagnose.py` |
| `user-ui` | `offcraft/ui/`、`assets/` |
| `server` | `server_mirror/__init__.py`、服务端入口与集成测试 |
| `server-mirror` | `server_mirror/config.py`、`routes.py`、`cache.py`、`proxy.py`、`__main__.py` |
| `server-deploy` | `DEPLOY.md`、`systemd/`、`docker/`、`caddy/`、`nginx/` |

`pyproject.toml`、`uv.lock`、`.python-version`、`.gitignore`、`.gitattributes`、`LICENSE` 为**公共文件**：内容在客户端各分支保持一致、服务端各分支保持一致；冲突时以集成分支为准，并重新 `uv lock`。

`offcraft/content/`（Modrinth 内容层：搜索 / 下载 / 依赖 / 整合包）归 `user-downloader` 所有；
`offcraft/ui/` 中的 `theme.py` / `widgets.py` / `content_page.py` / `app.py` 归 `user-ui` 所有。

## 合并流程

```bash
# 组件分支完成后
cd user
git fetch origin
git merge origin/user-downloader
uv run python -m unittest discover -s tests -v
git push
```

1. 组件分支定期 `git merge origin/user`（或 `origin/server`）保持同步；
2. 合并前必须跑测试；
3. 合并后推送集成分支，组件分支继续开发前先同步集成分支。

## 提交规范

- 前缀：`feat:`、`fix:`、`test:`、`docs:`、`chore:`、`refactor:`；
- 中文描述可，一句话说明“改了什么、为什么”；
- 不提交：虚拟环境、构建产物、缓存、日志、任何密钥/密码。

## 凭据

- 仓库中**不得出现**服务器密码、访问令牌、SSH 私钥；
- 本地通过环境变量传入：`OFFCRAFT_SSH_PASSWORD`、`OFFCRAFT_SSH_HOST`；
- 镜像访问令牌用 `MIRROR_TOKEN`，由部署环境注入。

## uv

所有 Python 分支统一：

```bash
uv sync --frozen
uv run python -m unittest discover -s tests -v
uv run python main.py
```

`uv.lock` 必须提交；依赖变更后重新 `uv lock` 并同步到同侧所有分支。
