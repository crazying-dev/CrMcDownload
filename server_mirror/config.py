"""服务端配置：全部来自环境变量，便于 systemd / Docker 注入。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["Settings"]

DEFAULT_UPSTREAMS = {
    "meta": "https://piston-meta.mojang.com",
    "data": "https://piston-data.mojang.com",
    "libs": "https://libraries.minecraft.net",
    "assets": "https://resources.download.minecraft.net",
    "fabric": "https://meta.fabricmc.net",
    # Fabric 加载器的 jar 依赖
    "fabricmaven": "https://maven.fabricmc.net",
    # Modrinth 搜索/版本 API（保留查询串）
    "modrinth": "https://api.modrinth.com",
    # Modrinth 文件 CDN（.jar / .mrpack / 图标）
    "modcdn": "https://cdn.modrinth.com",
}


@dataclass(slots=True)
class Settings:
    listen: str = "127.0.0.1:8081"
    cache_dir: Path = Path("./cache")
    token: str = ""
    metadata_ttl: int = 600
    proxy: str = ""
    upstreams: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_UPSTREAMS))
    max_cache_bytes: int = 0  # 0 = 不限

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        env = env or os.environ
        upstreams = dict(DEFAULT_UPSTREAMS)
        for key in list(upstreams):
            value = env.get(f"UPSTREAM_{key.upper()}")
            if value:
                upstreams[key] = value.rstrip("/")
        return cls(
            listen=env.get("MIRROR_LISTEN", "127.0.0.1:8081"),
            cache_dir=Path(env.get("MIRROR_CACHE_DIR", "./cache")),
            token=env.get("MIRROR_TOKEN", ""),
            metadata_ttl=int(env.get("MIRROR_METADATA_TTL", "600")),
            proxy=env.get("UPSTREAM_PROXY", ""),
            upstreams=upstreams,
            max_cache_bytes=int(env.get("MIRROR_MAX_CACHE_BYTES", "0")),
        )

    @property
    def host(self) -> str:
        return self.listen.rsplit(":", 1)[0]

    @property
    def port(self) -> int:
        return int(self.listen.rsplit(":", 1)[1])
