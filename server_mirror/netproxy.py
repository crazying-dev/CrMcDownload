"""上游请求：直连或经 SOCKS5 / HTTP CONNECT 代理。"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from urllib.parse import urlsplit

__all__ = ["UpstreamError", "open_upstream", "parse_proxy"]

_SOCKS_ERRORS = {
    1: "代理内部错误",
    2: "规则不允许",
    3: "网络不可达",
    4: "主机不可达",
    5: "连接被拒绝",
    6: "TTL 过期",
    7: "不支持的命令",
    8: "不支持的地址类型",
}


class UpstreamError(OSError):
    pass


def parse_proxy(value: str | None) -> tuple[str, str, int, str | None, str | None] | None:
    if not value:
        return None
    text = value if "://" in value else "http://" + value
    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https", "socks5", "socks5h"):
        raise ValueError(f"不支持的代理协议：{scheme}")
    default = {"http": 8080, "https": 443, "socks5": 1080, "socks5h": 1080}[scheme]
    return (
        scheme,
        parts.hostname or "",
        parts.port or default,
        parts.username,
        parts.password,
    )


def _socks5(
    host: str, port: int, proxy: tuple[str, str, int, str | None, str | None], timeout: float
) -> socket.socket:
    scheme, phost, pport, user, password = proxy
    sock = socket.create_connection((phost, pport), timeout=timeout)
    try:
        auth = bool(user)
        sock.sendall(b"\x05" + (b"\x02\x00\x02" if auth else b"\x01\x00"))
        version, method = _read(sock, 2)
        if version != 5:
            raise UpstreamError("SOCKS5 版本错误")
        if method == 2 and auth:
            u = (user or "").encode()
            p = (password or "").encode()
            sock.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
            if _read(sock, 2)[1] != 0:
                raise UpstreamError("SOCKS5 认证失败")
        elif method != 0:
            raise UpstreamError(f"SOCKS5 不支持的认证方式：{method}")

        remote_dns = scheme == "socks5h"
        if remote_dns or not _is_ip(host):
            addr = b"\x03" + bytes([len(host.encode())]) + host.encode()
        else:
            packed = ipaddress.ip_address(host)
            addr = (b"\x01" if packed.version == 4 else b"\x04") + packed.packed
        sock.sendall(b"\x05\x01\x00" + addr + port.to_bytes(2, "big"))
        version, reply, _, atyp = _read(sock, 4)
        if reply != 0:
            raise UpstreamError("SOCKS5：" + _SOCKS_ERRORS.get(reply, str(reply)))
        if atyp == 1:
            _read(sock, 4 + 2)
        elif atyp == 3:
            _read(sock, _read(sock, 1)[0] + 2)
        elif atyp == 4:
            _read(sock, 16 + 2)
        else:
            raise UpstreamError(f"SOCKS5 未知地址类型：{atyp}")
        return sock
    except BaseException:
        sock.close()
        raise


def _http_connect(
    host: str, port: int, proxy: tuple[str, str, int, str | None, str | None], timeout: float
) -> socket.socket:
    _scheme, phost, pport, user, password = proxy
    sock = socket.create_connection((phost, pport), timeout=timeout)
    try:
        target = f"{host}:{port}"
        lines = [f"CONNECT {target} HTTP/1.1", f"Host: {target}"]
        if user:
            import base64

            token = base64.b64encode(f"{user}:{password or ''}".encode()).decode()
            lines.append("Proxy-Authorization: Basic " + token)
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            chunk = sock.recv(4096)
            if not chunk:
                raise UpstreamError("代理提前关闭")
            buffer += chunk
        status = int(buffer.split(b" ", 2)[1])
        if status != 200:
            raise UpstreamError(f"CONNECT 失败：{status}")
        return sock
    except BaseException:
        sock.close()
        raise


def open_upstream(url: str, proxy_text: str | None, timeout: float = 30.0):
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    proxy = parse_proxy(proxy_text)
    if proxy is None:
        raw = socket.create_connection((host, port), timeout=timeout)
    elif proxy[0] in ("socks5", "socks5h"):
        raw = _socks5(host, port, proxy, timeout)
    elif proxy[0] in ("http", "https"):
        raw = _http_connect(host, port, proxy, timeout)
    else:
        raise UpstreamError(f"不支持的代理：{proxy[0]}")
    if parts.scheme == "https":
        raw = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    conn.sock = raw
    return conn


def _read(sock: socket.socket, count: int) -> bytes:
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise UpstreamError("代理提前关闭")
        data += chunk
    return data


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False
