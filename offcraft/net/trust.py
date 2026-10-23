"""下载地址信任策略：只允许 HTTPS + 已知白名单域。

镜像本身只承认 :data:`offcraft.mirror.HOST_ROUTES` 里的上游，但整合包
（``.mrpack``）的清单是第三方数据，里面的 ``downloads`` 可能是任意地址。
直接照着下载等于把「下载并执行/加载什么」的控制权交给了整合包作者，
因此这里统一做一次判断：

* 明文 ``http://`` 一律拒绝——中间人可以直接替换文件内容；
* 主机必须属于白名单域（Mojang / Fabric / Forge / NeoForge / Modrinth /
  GitHub Releases / CurseForge CDN 等）；
* 其余地址默认拒绝，只有用户显式打开「允许未知来源」才放行。

本模块只做判断，不发请求，便于单独测试。
"""

from __future__ import annotations

from urllib.parse import urlsplit

from offcraft.mirror import HOST_ROUTES

__all__ = [
    "TRUSTED_DOMAINS",
    "TRUSTED_HOSTS",
    "check_url",
    "host_of",
    "is_https",
    "is_trusted_host",
]

#: 额外信任的精确主机（镜像路由表里的主机自动纳入）
_EXTRA_HOSTS = {
    # Modrinth / GitHub Release 资产
    "github.com",
    "api.github.com",
    "objects.githubusercontent.com",
    "codeload.github.com",
    "raw.githubusercontent.com",
    "avatars.githubusercontent.com",
    # CurseForge 文件 CDN
    "edge.forgecdn.net",
    "mediafilez.forgecdn.net",
    "media.forgecdn.net",
    "files.forgecdn.net",
}

#: 额外信任的域名后缀（含所有子域）
TRUSTED_DOMAINS: tuple[str, ...] = (
    "mojang.com",
    "minecraft.net",
    "fabricmc.net",
    "minecraftforge.net",
    "neoforged.net",
    "modrinth.com",
    "githubusercontent.com",
    "forgecdn.net",
)

#: 精确主机白名单（小写）
TRUSTED_HOSTS: frozenset[str] = frozenset(HOST_ROUTES) | frozenset(_EXTRA_HOSTS)


def host_of(url: str) -> str:
    """URL 的主机名（小写）；解析失败返回空串。"""
    try:
        return (urlsplit(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""


def is_https(url: str) -> bool:
    """是否为合法的 ``https://`` 地址。"""
    try:
        parts = urlsplit(str(url or ""))
    except ValueError:
        return False
    return parts.scheme.lower() == "https" and bool(parts.hostname)


def is_trusted_host(host: str) -> bool:
    """主机是否在白名单里（精确匹配或命中受信域名后缀）。"""
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return False
    if host in TRUSTED_HOSTS:
        return True
    return any(host == domain or host.endswith("." + domain) for domain in TRUSTED_DOMAINS)


def check_url(url: str, *, allow_untrusted: bool = False) -> str | None:
    """判断能否直接下载该地址。

    允许时返回 ``None``；拒绝时返回一句面向用户的理由。
    ``allow_untrusted=True`` 只放行「HTTPS + 未知主机」，明文 HTTP 永远拒绝。
    """
    text = str(url or "").strip()
    if not text:
        return "下载地址为空"
    try:
        parts = urlsplit(text)
    except ValueError:
        return f"下载地址无法解析：{text[:80]}"
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if scheme not in ("http", "https") or not host:
        return f"不支持的下载协议：{text[:80]}"
    if scheme != "https":
        return f"拒绝明文 HTTP 下载地址：{host}"
    if is_trusted_host(host):
        return None
    if allow_untrusted:
        return None
    return f"未知下载源 {host}（可在设置里允许未知来源）"
