"""``python -m server_mirror`` 入口。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from server_mirror.api import ApiRouter
from server_mirror.config import Settings
from server_mirror.db import Database
from server_mirror.proxy import MirrorHTTPServer
from server_mirror.skins import SkinStore
from server_mirror.servers import ServerProbe
from server_mirror.static import StaticHandler

if TYPE_CHECKING:  # pragma: no cover - 仅类型提示
    from server_mirror.p2p import RelayServer, SignalingRouter

__all__ = ["main"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="server_mirror", description="CrMcDownload 镜像服务")
    parser.add_argument("--listen", help="监听地址，默认 127.0.0.1:8081")
    parser.add_argument("--cache-dir", help="缓存目录")
    parser.add_argument("--token", help="访问令牌（X-OffCraft-Token）")
    parser.add_argument("--ttl", type=int, help="元数据缓存秒数")
    parser.add_argument("--proxy", help="上游代理，如 socks5h://127.0.0.1:1080")
    return parser


def _build_p2p(settings: Settings) -> tuple["SignalingRouter | None", "RelayServer | None"]:
    """启用离线联机时构造信令 + 中继；否则 ``(None, None)``。"""
    if not getattr(settings, "p2p_enable", False):
        return None, None
    from server_mirror.p2p import RelayServer, SignalingRouter

    relay = RelayServer(settings)
    p2p_router = SignalingRouter(settings=settings, relay=relay)
    return p2p_router, relay


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

    db = Database(settings.db_path)
    skins = SkinStore(settings.skins_dir)
    page = StaticHandler(settings.page_dir)
    probe = (
        ServerProbe(
            db,
            interval=settings.probe_interval,
            concurrency=max(1, settings.max_upstreams),
            popular_path=settings.popular_data,
        )
        if settings.probe_enable
        else None
    )
    router = ApiRouter(
        settings=settings,
        db=db,
        skins=skins,
        page=page,
        metrics_snapshot=None,   # 注入后填充
        cache_stats=None,
        probe_status=probe.status if probe else None,
    )
    p2p_router, relay = _build_p2p(settings)
    router.p2p = p2p_router
    server = MirrorHTTPServer(
        (settings.host, settings.port), settings, router=router, db=db, probe=probe, relay=relay
    )
    # 指标与缓存统计需要 server 先存在，构造后再回填
    router.metrics_snapshot = server.metrics.snapshot
    router.cache_stats = server.cache.stats
    if probe is not None:
        probe.start()
    if relay is not None:
        relay.start()
    print(
        f"mirror listening on http://{settings.host}:{settings.port} "
        f"cache={settings.cache_dir} token={'on' if settings.token else 'off'} "
        f"db={settings.db_path} probe={'on' if probe else 'off'} "
        f"p2p={'on' if p2p_router else 'off'}",
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
