"""``python -m server_mirror`` 入口。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from server_mirror.config import Settings
from server_mirror.proxy import MirrorHTTPServer

__all__ = ["main"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="server_mirror", description="CrMcDownload 镜像服务")
    parser.add_argument("--listen", help="监听地址，默认 127.0.0.1:8081")
    parser.add_argument("--cache-dir", help="缓存目录")
    parser.add_argument("--token", help="访问令牌（X-OffCraft-Token）")
    parser.add_argument("--ttl", type=int, help="元数据缓存秒数")
    parser.add_argument("--proxy", help="上游代理，如 socks5h://127.0.0.1:1080")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.from_env()
    if args.listen:
        settings.listen = args.listen
    if args.cache_dir:
        settings.cache_dir = Path(args.cache_dir)
    if args.token is not None:
        settings.token = args.token
    if args.ttl:
        settings.metadata_ttl = args.ttl
    if args.proxy:
        settings.proxy = args.proxy

    server = MirrorHTTPServer((settings.host, settings.port), settings)
    print(
        f"mirror listening on http://{settings.host}:{settings.port} "
        f"cache={settings.cache_dir} token={'on' if settings.token else 'off'}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
