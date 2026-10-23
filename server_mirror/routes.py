"""路由与路径白名单：只允许已知前缀，拒绝穿越与绝对路径。

除原版资源外还代理 Modrinth（API + 文件 CDN）与 Fabric Maven：
Modrinth 的搜索接口依赖查询串，因此 :class:`Route` 会一并保留 query，
并用 query 的哈希区分缓存文件。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

__all__ = ["BadPath", "Route", "resolve"]

_PREFIXES = (
    "meta",
    "data",
    "libs",
    "assets",
    "fabric",
    "fabricmaven",
    "modrinth",
    "modcdn",
)
_BAD_CHARS = re.compile(r"[\x00-\x1f\x7f\\]")
_MAX_PATH = 2048
_MAX_QUERY = 2048


class BadPath(ValueError):
    """请求路径不合法。"""


@dataclass(frozen=True, slots=True)
class Route:
    prefix: str
    relative: str  # 已解码、去掉前导斜杠的相对路径
    upstream_base: str
    query: str = ""

    @property
    def cache_key(self) -> str:
        if not self.query:
            return f"{self.prefix}/{self.relative}"
        digest = hashlib.sha1(self.query.encode("utf-8")).hexdigest()[:16]
        return f"{self.prefix}/{self.relative}.q{digest}"

    @property
    def upstream_path(self) -> str:
        path = "/" + quote(self.relative, safe="/")
        if self.query:
            return f"{path}?{self.query}"
        return path


def resolve(raw_path: str, upstreams: dict[str, str]) -> Route:
    if not raw_path.startswith("/"):
        raise BadPath("路径必须以 / 开头")
    if len(raw_path) > _MAX_PATH:
        raise BadPath("路径过长")
    if _BAD_CHARS.search(raw_path):
        raise BadPath("路径包含非法字符")
    split = urlsplit(raw_path)
    query = split.query
    if len(query) > _MAX_QUERY:
        raise BadPath("查询串过长")
    if _BAD_CHARS.search(query):
        raise BadPath("查询串包含非法字符")
    path = unquote(split.path)
    if "//" in path:
        raise BadPath("路径包含空段")
    parts = [part for part in path.split("/") if part]
    if not parts:
        raise BadPath("缺少路由前缀")
    prefix = parts[0].lower()
    if prefix not in _PREFIXES:
        raise BadPath(f"未知路由前缀：{prefix}")
    if any(part in (".", "..") for part in parts[1:]):
        raise BadPath("路径包含 . 或 ..")
    if any(":" in part for part in parts):
        raise BadPath("路径包含冒号")
    relative = "/".join(parts[1:])
    if not relative:
        raise BadPath("缺少文件路径")
    base = upstreams.get(prefix)
    if not base:
        raise BadPath(f"路由 {prefix} 未配置上游")
    return Route(
        prefix=prefix,
        relative=relative,
        upstream_base=base.rstrip("/"),
        query=query,
    )
