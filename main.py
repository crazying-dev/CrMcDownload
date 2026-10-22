"""CrMCDownload 镜像服务端入口。

启动方式：

    uv run --frozen main.py

等价于 ``python -m server_mirror``，但作为独立入口更适合 systemd / Docker。
"""

from __future__ import annotations

import sys

from server_mirror.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
