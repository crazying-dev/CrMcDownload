"""服务端配置：全部来自环境变量，便于 systemd / Docker 注入。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["Settings"]


def _int(env: dict[str, str], name: str, default: int) -> int:
    """容错地读整数环境变量（非法值回落默认值，不把服务启崩）。"""
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def _float(env: dict[str, str], name: str, default: float) -> float:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default

DEFAULT_UPSTREAMS = {
    "meta": "https://piston-meta.mojang.com",
    "data": "https://piston-data.mojang.com",
    "libs": "https://libraries.minecraft.net",
    "assets": "https://resources.download.minecraft.net",
    "fabric": "https://meta.fabricmc.net",
    # Fabric 加载器的 jar 依赖
    "fabricmaven": "https://maven.fabricmc.net",
    # Forge / NeoForge maven（maven-metadata.xml 与安装器依赖）
    "forgemaven": "https://maven.minecraftforge.net",
    "neoforgemaven": "https://maven.neoforged.net",
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
    #: 单个文件超过这个大小就不进缓存（仍会正常转发），默认 1 GiB
    max_file_bytes: int = 1024 * 1024 * 1024
    #: 同时进行的上游回源请求上限，0 表示不限
    max_upstreams: int = 8
    #: 同一个键正在回源时，其他请求最多等多久（秒）
    flight_wait: float = 60.0
    #: 上游并发名额的等待时间（秒），拿不到就返回 503
    upstream_wait: float = 30.0
    #: 过期缓存清理的最小间隔（秒）
    sweep_interval: float = 300.0

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
            metadata_ttl=_int(env, "MIRROR_METADATA_TTL", 600),
            proxy=env.get("UPSTREAM_PROXY", ""),
            upstreams=upstreams,
            max_cache_bytes=_int(env, "MIRROR_MAX_CACHE_BYTES", 0),
            max_file_bytes=_int(env, "MIRROR_MAX_FILE_BYTES", 1024 * 1024 * 1024),
            max_upstreams=_int(env, "MIRROR_MAX_UPSTREAM", 8),
            flight_wait=_float(env, "MIRROR_FLIGHT_WAIT", 60.0),
            upstream_wait=_float(env, "MIRROR_UPSTREAM_WAIT", 30.0),
            sweep_interval=_float(env, "MIRROR_SWEEP_INTERVAL", 300.0),
        )

    @property
    def host(self) -> str:
        return self.listen.rsplit(":", 1)[0]

    @property
    def port(self) -> int:
        return int(self.listen.rsplit(":", 1)[1])
