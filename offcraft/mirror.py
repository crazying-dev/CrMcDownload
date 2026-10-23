"""上游 URL → 镜像地址的映射。

客户端只与 ``download_base`` 通信：所有 Mojang / Fabric 的原始 URL 都会
在这里被改写成 ``{base}/<route>/<path>``。未知主机一律报错，绝不回落到直连。
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

__all__ = [
    "DirectSource",
    "HOST_ROUTES",
    "Mirror",
    "UnknownUpstreamError",
]


class UnknownUpstreamError(ValueError):
    """URL 的主机不在镜像映射表中。"""

    def __init__(self, url: str) -> None:
        super().__init__(
            f"未识别的上游地址：{url}\n"
            "请确认这是 Mojang/Fabric 官方地址，或在服务端新增对应映射。"
        )
        self.url = url


#: 上游主机 → 镜像路由前缀
HOST_ROUTES: dict[str, str] = {
    "piston-meta.mojang.com": "meta",
    "launchermeta.mojang.com": "meta",
    "piston-data.mojang.com": "data",
    "launcher.mojang.com": "data",
    "libraries.minecraft.net": "libs",
    "resources.download.minecraft.net": "assets",
    "meta.fabricmc.net": "fabric",
    "maven.fabricmc.net": "fabricmaven",
    # Forge / NeoForge maven（安装器与 maven-metadata.xml）
    "maven.minecraftforge.net": "forgemaven",
    "maven.neoforged.net": "neoforgemaven",
    # Modrinth API / 文件 CDN（搜索依赖查询串，镜像服务原样转发）
    "api.modrinth.com": "modrinth",
    "cdn.modrinth.com": "modcdn",
}


@dataclass(frozen=True, slots=True)
class DirectSource:
    """官方源直连（开发/可直连网络时使用），不改写 URL。"""

    base: str = "https://mojang.com"

    @property
    def base_url(self) -> str:
        return self.base

    def rewrite(self, url: str) -> str:
        return url

    def rewrite_optional(self, url: str | None) -> str | None:
        return url

    def is_mirror_url(self, url: str) -> bool:  # noqa: ARG002 - 接口一致性
        return False


@dataclass(frozen=True, slots=True)
class Mirror:
    """把官方 URL 改写到自建镜像。

    >>> Mirror("https://mc.example.com").rewrite(
    ...     "https://libraries.minecraft.net/org/lwjgl/lwjgl/3.3.3/lwjgl-3.3.3.jar"
    ... )
    'https://mc.example.com/libs/org/lwjgl/lwjgl/3.3.3/lwjgl-3.3.3.jar'
    """

    base: str

    def __post_init__(self) -> None:
        if not self.base.startswith(("http://", "https://")):
            raise ValueError(f"download_base 必须是 http(s) 地址：{self.base!r}")

    @property
    def base_url(self) -> str:
        return self.base.rstrip("/")

    def rewrite(self, url: str) -> str:
        """把官方 URL 改写为镜像 URL；未知主机抛 :class:`UnknownUpstreamError`。"""
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        route = HOST_ROUTES.get(host)
        if route is None:
            raise UnknownUpstreamError(url)
        path = parts.path or "/"
        target = f"{self.base_url}/{route}{path}"
        if parts.query:
            target = f"{target}?{parts.query}"
        return target

    def rewrite_optional(self, url: str | None) -> str | None:
        return None if url is None else self.rewrite(url)

    def is_mirror_url(self, url: str) -> bool:
        return url.startswith(self.base_url + "/")

    def __str__(self) -> str:  # pragma: no cover - 展示用
        return self.base_url
