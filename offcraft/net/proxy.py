"""最小代理实现：SOCKS5（含用户名密码）与 HTTP CONNECT。

只依赖标准库，供下载器与服务端上游请求共用。
``socks5h://`` 由代理端解析域名，``socks5://`` 由本地解析。
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

__all__ = [
    "ProxyConfig",
    "ProxyError",
    "open_tunnel",
    "parse_proxy",
]


class ProxyError(OSError):
    """代理握手或连接失败。"""


@dataclass(frozen=True, slots=True)
class ProxyConfig:
    scheme: str  # http / https / socks5 / socks5h
    host: str
    port: int
    username: str | None = None
    password: str | None = None

    @property
    def remote_dns(self) -> bool:
        return self.scheme == "socks5h"

    def describe(self) -> str:
        auth = f"{self.username}@" if self.username else ""
        return f"{self.scheme}://{auth}{self.host}:{self.port}"


_DEFAULT_PORTS = {"http": 8080, "https": 443, "socks5": 1080, "socks5h": 1080}


def parse_proxy(value: str | None) -> ProxyConfig | None:
    """解析代理配置字符串；空值返回 ``None``。"""
    if not value:
        return None
    text = value.strip()
    if "://" not in text:
        text = "http://" + text
    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise ValueError(f"不支持的代理协议：{scheme}（支持 http/https/socks5/socks5h）")
    if not parts.hostname:
        raise ValueError(f"代理地址缺少主机：{value!r}")
    try:
        port = parts.port or _DEFAULT_PORTS[scheme]
    except ValueError as exc:
        raise ValueError(f"代理端口非法：{value!r}") from exc
    return ProxyConfig(
        scheme=scheme,
        host=parts.hostname,
        port=port,
        username=unquote(parts.username) if parts.username else None,
        password=unquote(parts.password) if parts.password else None,
    )


def open_tunnel(
    host: str,
    port: int,
    proxy: ProxyConfig | None,
    timeout: float = 30.0,
) -> socket.socket:
    """建立到 ``host:port`` 的 TCP 连接（必要时经代理）。"""
    if proxy is None:
        return socket.create_connection((host, port), timeout=timeout)
    if proxy.scheme in ("socks5", "socks5h"):
        return _socks5_connect(host, port, proxy, timeout)
    if proxy.scheme == "http":
        return _http_connect(host, port, proxy, timeout)
    raise ProxyError(f"暂不支持 {proxy.scheme} 代理")


def _socks5_connect(host: str, port: int, proxy: ProxyConfig, timeout: float) -> socket.socket:
    sock = socket.create_connection((proxy.host, proxy.port), timeout=timeout)
    try:
        use_auth = bool(proxy.username)
        methods = b"\x00\x02" if use_auth else b"\x00"
        sock.sendall(b"\x05" + bytes([len(methods)]) + methods)
        version, method = _recv_exact(sock, 2)
        if version != 5:
            raise ProxyError(f"SOCKS5 版本错误：{version}")
        if method == 0x02 and use_auth:
            user = (proxy.username or "").encode()
            password = (proxy.password or "").encode()
            sock.sendall(b"\x01" + bytes([len(user)]) + user + bytes([len(password)]) + password)
            _, status = _recv_exact(sock, 2)
            if status != 0:
                raise ProxyError("SOCKS5 用户名/密码认证失败")
        elif method != 0x00:
            raise ProxyError(f"SOCKS5 不支持的认证方式：0x{method:02x}")

        if proxy.remote_dns or not _is_ip(host):
            address = b"\x03" + bytes([len(host.encode())]) + host.encode()
        else:
            packed = ipaddress.ip_address(host)
            address = (b"\x01" if packed.version == 4 else b"\x04") + packed.packed

        sock.sendall(b"\x05\x01\x00" + address + port.to_bytes(2, "big"))
        version, reply, _, atyp = _recv_exact(sock, 4)
        if version != 5:
            raise ProxyError(f"SOCKS5 响应版本错误：{version}")
        if reply != 0:
            raise ProxyError(f"SOCKS5 连接失败：{_SOCKS_ERRORS.get(reply, f'错误码 {reply}')}")
        if atyp == 0x01:
            _recv_exact(sock, 4 + 2)
        elif atyp == 0x04:
            _recv_exact(sock, 16 + 2)
        elif atyp == 0x03:
            length = _recv_exact(sock, 1)[0]
            _recv_exact(sock, length + 2)
        else:
            raise ProxyError(f"SOCKS5 未知地址类型：{atyp}")
        return sock
    except BaseException:
        sock.close()
        raise


_SOCKS_ERRORS = {
    1: "代理内部错误",
    2: "规则不允许连接",
    3: "网络不可达",
    4: "主机不可达",
    5: "连接被拒绝",
    6: "TTL 过期",
    7: "不支持的命令",
    8: "不支持的地址类型",
}


def _http_connect(host: str, port: int, proxy: ProxyConfig, timeout: float) -> socket.socket:
    sock = socket.create_connection((proxy.host, proxy.port), timeout=timeout)
    try:
        target = f"{host}:{port}"
        lines = [f"CONNECT {target} HTTP/1.1", f"Host: {target}", "Proxy-Connection: keep-alive"]
        if proxy.username:
            import base64

            raw = f"{proxy.username}:{proxy.password or ''}".encode()
            lines.append("Proxy-Authorization: Basic " + base64.b64encode(raw).decode())
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            chunk = sock.recv(4096)
            if not chunk:
                raise ProxyError("HTTP 代理提前关闭连接")
            buffer += chunk
            if len(buffer) > 65536:
                raise ProxyError("HTTP 代理响应头过大")
        status_line = buffer.split(b"\r\n", 1)[0].decode("latin-1")
        parts = status_line.split()
        if len(parts) < 2 or not parts[1].isdigit():
            raise ProxyError(f"HTTP 代理响应异常：{status_line!r}")
        code = int(parts[1])
        if code != 200:
            raise ProxyError(f"HTTP 代理 CONNECT 失败：{status_line}")
        return sock
    except BaseException:
        sock.close()
        raise


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise ProxyError("SOCKS5 代理提前关闭连接")
        data += chunk
    return data


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False
