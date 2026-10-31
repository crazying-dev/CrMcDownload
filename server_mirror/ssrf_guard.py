"""服务器地址解析与 SSRF 护栏（服务端探测与 API 上报共用）。

与客户端 ``offcraft.servers.ssrf`` 同一套规则的精简版：只接受
``host`` / ``host:port`` / ``[v6]:port``，拒绝 URL、凭据、路径与非法字符；
公网判定排除回环 / 私网 / 链路本地 / 组播 / 保留 / CGNAT(100.64/10) / IPv6 ULA。
"""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlsplit

__all__ = ["DEFAULT_PORT", "ServerAddressError", "ip_is_public", "parse_host_port", "resolve_is_public"]

DEFAULT_PORT = 25565
_LABEL = re.compile(r"^[A-Za-z0-9_-]{1,63}$")
_MAX_HOSTNAME = 253
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_ULA = ipaddress.ip_network("fc00::/7")


class ServerAddressError(ValueError):
    """地址不合法。"""


def parse_host_port(text: str, *, default_port: int = DEFAULT_PORT) -> tuple[str, int]:
    raw = str(text or "").strip()
    if not raw:
        raise ServerAddressError("服务器地址不能为空")
    if any(char.isspace() for char in raw):
        raise ServerAddressError("服务器地址不能包含空格")
    if "://" in raw or raw.startswith("//"):
        raise ServerAddressError("只需主机名与端口，不要带 scheme")
    parts = urlsplit("//" + raw)
    try:
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port
    except ValueError as exc:
        raise ServerAddressError("端口不合法") from exc
    if parts.username or "@" in raw:
        raise ServerAddressError("地址不能包含账号信息")
    if parts.path.strip("/"):
        raise ServerAddressError("地址不能带路径")
    if not host:
        raise ServerAddressError("服务器地址不能为空")
    if ":" in host:
        try:
            ipaddress.IPv6Address(host)
        except ValueError as exc:
            raise ServerAddressError("IPv6 地址不合法") from exc
    else:
        if len(host) > _MAX_HOSTNAME:
            raise ServerAddressError("服务器地址过长")
        labels = host.split(".")
        if any(label.startswith("-") or label.endswith("-") for label in labels):
            raise ServerAddressError("主机名不合法")
        if not all(_LABEL.match(label) for label in labels):
            raise ServerAddressError("主机名不合法")
    if port is None:
        port = default_port
    if not 1 <= port <= 65535:
        raise ServerAddressError("端口需在 1-65535 之间")
    return host, int(port)


def ip_is_public(ip: ipaddress.IPvAddress) -> bool:
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        return False
    if ip in _CGNAT:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip in _ULA:
        return False
    return not ip.is_private


def resolve_is_public(host: str) -> tuple[bool, str]:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        return False, f"无法解析：{exc.strerror or exc}"
    if not infos:
        return False, "无法解析主机"
    for info in infos:
        try:
            address = ipaddress.ip_address(str(info[4][0]))
        except ValueError:
            continue
        if not ip_is_public(address):
            return False, f"拒绝非公网地址 {address}"
    return True, ""
