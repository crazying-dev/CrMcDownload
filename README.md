# user-launcher（CrMcDownload 客户端·启动）

负责“把游戏跑起来”：

- `offcraft/launch/libraries.py`：classpath 与库规则
- `offcraft/launch/natives.py`：natives 解压（路径穿越防护）
- `offcraft/launch/java.py`：Java 探测与运行时下载
- `offcraft/launch/auth.py`：离线账户（UUID v3）
- `offcraft/launch/command.py`：占位符展开与参数拼装
- `offcraft/launch/process.py`：子进程、日志采集、退出处理
- `offcraft/diagnose.py`：启动前检查

## 开发

```bash
uv sync --frozen
uv run python -m unittest discover -s tests -v
```

合并方向 `user-launcher → user`。
