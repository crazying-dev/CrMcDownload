"""服务器地址解析与安全护栏。

「联机」页允许用户输入任意地址并主动发起 TCP 连接，这是本项目第一次（也是
唯一一次）让客户端去连一个不在镜像白名单里的主机。为了避免把它变成
SSRF 跳板（扫内网、打自己局域网的 22 端口、探测路由器管理页），这里做三件事：

1. 地址语法校验：只接受 ``host`` / ``host:port`` / ``[v6]:port``，拒绝 URL、
   空白、``@``、非法字符与越界端口；
2. 目标必须是公网地址：回环、私网、链路本地、组播、保留段一律拒绝，
   除非用户显式打开 ``allow_local_servers``（局域网联机场景）；
3. 端口范围限制：默认 25565，允许 1-65535，但拒绝 0 与常见服务端口之外的
   明文协议混淆写法。

本模块只做判断，不发请求，便于单独测试。
"""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlsplit

__all__ = [
    "DEFAULT_PORT",
    "MAX_BATCH",
    "addr_of",
    "format_host_port",
    "is_public_ip",
    "parse_server",
    "resolve_is_public",
]

#: Minecraft Java 版默认端口
DEFAULT_PORT = 25565
#: 一轮批量探测的服务器数上限（避免用户把搜索词写成一大串地址打爆网络）
MAX_BATCH = 64

_LABEL = re.compile(r"^[A-Za-z0-9_-]{1,63}$")
_MAX_HOSTNAME = 253

#: 运营商级 NAT（100.64.0.0/10）与 IPv6 唯一本地地址（fc00::/7）：
#: Python 3.12 的 ``is_private`` 已涵盖大部分，但 CGNAT 段需要显式判定。
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_ULA = ipaddress.ip_network("fc00::/7")


class ServerAddressError(ValueError):
    """地址不合法（面向用户的中文理由）。"""


def parse_server(text: str, *, default_port: int = DEFAULT_PORT) -> tuple[str, int]:
    """把用户输入的服务器地址拆成 ``(host, port)``。

    支持 ``mc.example.com``、``mc.example.com:25565``、``[::1]:25565``、``1.2.3.4``；
    拒绝 URL（含 scheme）、空主机、非法字符与越界端口。主机名统一小写。
    """
    raw = str(text or "").strip()
    if not raw:
        raise ServerAddressError("服务器地址不能为空")
    if any(char.isspace() for char in raw):
        raise ServerAddressError("服务器地址不能包含空格")
    if "://" in raw or raw.startswith("//"):
        raise ServerAddressError("只需填主机名与端口，不要带 http(s):// 前缀")
    parts = urlsplit("//" + raw)
    try:
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port
    except ValueError as exc:
        raise ServerAddressError(f"端口不合法：{raw}") from exc
    if parts.username or "@" in raw:
        raise ServerAddressError("服务器地址不能包含账号信息（@ 前缀）")
    if parts.path.strip("/"):
        raise ServerAddressError("服务器地址不能带路径，只接受 主机[:端口]")
    if not host:
        raise ServerAddressError("服务器地址不能为空")
    if ":" in host:  # IPv6 字面量
        try:
            ipaddress.IPv6Address(host)
        except ValueError as exc:
            raise ServerAddressError(f"IPv6 地址不合法：{host}") from exc
    else:
        if len(host) > _MAX_HOSTNAME:
            raise ServerAddressError("服务器地址过长")
        if any(label.startswith("-") or label.endswith("-") for label in host.split(".")):
            raise ServerAddressError(f"主机名不合法：{host}")
        if not all(_LABEL.match(label) for label in host.split(".")):
            raise ServerAddressError(f"主机名不合法：{host}")
    if port is None:
        port = default_port
    if not 1 <= port <= 65535:
        raise ServerAddressError(f"端口需在 1-65535 之间：{port}")
    return host, int(port)


def format_host_port(host: str, port: int, *, default_port: int = DEFAULT_PORT) -> str:
    """展示用的地址串：IPv6 加方括号，默认端口省略。"""
    inner = f"[{host}]" if ":" in host else host
    return inner if port == default_port else f"{inner}:{port}"


def is_public_ip(host: str) -> bool:
    """是否为可公网访问的地址（字面量 IP 直接判定，主机名交给解析结果）。"""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True  # 主机名，需进一步解析
    return _ip_is_public(ip)


def resolve_is_public(host: str) -> tuple[bool, str]:
    """解析主机名并判断是否公网可达。

    返回 ``(是否允许, 理由)``：允许时理由为空串；拒绝时是面向用户的中文说明。
    解析失败本身也算拒绝（无法确认目标就不是可信地址）。
    """
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if _ip_is_public(ip):
            return True, ""
        return False, _reason(ip)
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        return False, f"无法解析服务器地址：{host}（{exc.strerror or exc}）"
    if not infos:
        return False, f"无法解析服务器地址：{host}"
    for info in infos:
        try:
            address = ipaddress.ip_address(str(info[4][0]))
        except ValueError:
            continue
        if not _ip_is_public(address):
            return False, _reason(address)
    return True, ""


def addr_of(host: str, port: int) -> tuple[str, int]:
    """socket.connect 需要的地址元组（原样返回，校验在调用前完成）。"""
    return host, int(port)


def _reason(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    if ip.is_loopback:
        return "拒绝连接本机回环地址（可在设置里允许本地/内网服务器）"
    if ip.is_link_local:
        return "拒绝连接链路本地地址（169.254.x.x / fe80::）"
    if ip.is_multicast:
        return "拒绝连接组播地址"
    if ip.is_unspecified:
        return "拒绝连接未指定地址"
    if ip.is_reserved:
        return "拒绝连接保留地址段"
    version = "IPv6 唯一本地" if isinstance(ip, ipaddress.IPv6Address) and ip in _ULA else "内网"
    return f"拒绝连接{version}地址 {ip}（可在设置里允许本地/内网服务器）"


def _ip_is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """非回环、非私网、非运营商 NAT、非链路本地、非组播、非保留 —— 才算可以直连。"""
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        return False
    if ip in _CGNAT:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip in _ULA:
        return False
    return not ip.is_private
