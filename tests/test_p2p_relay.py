"""``RelayServer`` 端到端：令牌成对拼桥、双向密文转发、非法/超限断开、干净停止。

真起 asyncio 中继在回环空闲端口上（仅本机 socket，不出网），验证配对协程改造
后两条入站连接确实成桥、字节封顶会断开。
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass

import pytest

from server_mirror.p2p import RelayServer


@dataclass
class _Settings:
    p2p_relay_port: int = 0
    p2p_relay_host: str = "127.0.0.1"
    p2p_room_ttl: int = 180
    p2p_relay_bytes_cap: int = 512 * 1024 * 1024
    host: str = "127.0.0.1"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_ready(port: int, timeout: float = 3.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            probe = socket.create_connection(("127.0.0.1", port), timeout=0.5)
            probe.close()
            return
        except OSError:
            time.sleep(0.02)
    raise AssertionError("中继未在预期时间内就绪")


def _hello(sock: socket.socket, token: str) -> None:
    sock.sendall(f"HELLO {token}\n".encode("ascii"))


def _read_line(sock: socket.socket) -> bytes:
    # 逐字节读到换行，避免 makefile 预读把后续转发字节吞进缓冲
    buf = bytearray()
    while not buf.endswith(b"\n"):
        chunk = sock.recv(1)
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


@pytest.fixture()
def relay():
    port = _free_port()
    server = RelayServer(_Settings(), host="127.0.0.1", port=port)
    server.start()
    _wait_ready(port)
    try:
        yield server, port
    finally:
        server.stop()


def test_relay_pairs_and_forwards_bytes(relay):
    server, port = relay
    token = server.new_token("ROOM", requester="a" * 32, target="b" * 32)

    # 先到者：连上并 HELLO，阻塞等待配对（OK 在后到者到来时统一回）
    first = socket.create_connection(("127.0.0.1", port), timeout=5)
    first.settimeout(5)
    _hello(first, token)
    second = socket.create_connection(("127.0.0.1", port), timeout=5)
    second.settimeout(5)
    _hello(second, token)

    try:
        assert _read_line(second).strip() == b"OK"
        assert _read_line(first).strip() == b"OK"
        # 双向透传不透明字节
        first.sendall(b"client->server")
        assert second.recv(64) == b"client->server"
        second.sendall(b"server->client")
        assert first.recv(64) == b"server->client"
        assert server.bridged == 1
    finally:
        first.close()
        second.close()


def test_relay_rejects_unknown_token(relay):
    _server, port = relay
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock.settimeout(5)
    try:
        _hello(sock, "not-a-real-token")
        reply = _read_line(sock)
        assert reply.startswith(b"ERR")
        assert "unknown-token" in reply.decode()
    finally:
        sock.close()


def test_relay_rejects_malformed_handshake(relay):
    _server, port = relay
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock.settimeout(5)
    try:
        sock.sendall(b"GARBAGE\r\n")
        reply = _read_line(sock)
        assert reply.startswith(b"ERR")
        assert "bad-handshake" in reply.decode()
    finally:
        sock.close()


def test_relay_token_is_single_use(relay):
    server, port = relay
    token = server.new_token("ROOM", requester="a" * 32, target="b" * 32)
    first = socket.create_connection(("127.0.0.1", port), timeout=5)
    first.settimeout(5)
    _hello(first, token)
    second = socket.create_connection(("127.0.0.1", port), timeout=5)
    second.settimeout(5)
    _hello(second, token)
    try:
        assert _read_line(second).strip() == b"OK"
        assert _read_line(first).strip() == b"OK"
        # 桥接完成后令牌已被消费：第三条同令牌连接被拒
        third = socket.create_connection(("127.0.0.1", port), timeout=5)
        third.settimeout(5)
        try:
            _hello(third, token)
            reply = _read_line(third)
            assert reply.startswith(b"ERR")
        finally:
            third.close()
    finally:
        first.close()
        second.close()


def test_relay_bytes_cap_disconnects():
    port = _free_port()
    server = RelayServer(_Settings(), host="127.0.0.1", port=port, bytes_cap=1024)
    server.start()
    _wait_ready(port)
    token = server.new_token("ROOM", requester="a" * 32, target="b" * 32)
    try:
        first = socket.create_connection(("127.0.0.1", port), timeout=5)
        first.settimeout(5)
        _hello(first, token)
        second = socket.create_connection(("127.0.0.1", port), timeout=5)
        second.settimeout(5)
        _hello(second, token)
        assert _read_line(second).strip() == b"OK"
        assert _read_line(first).strip() == b"OK"
        # 灌入远超封顶的字节：中继应断开（读端最终收到 EOF）
        payload = b"x" * 4096
        stop = threading.Event()

        def flood():
            while not stop.is_set():
                try:
                    first.sendall(payload)
                except OSError:
                    break

        t = threading.Thread(target=flood, daemon=True)
        t.start()
        try:
            drained = 0
            deadline = time.time() + 5
            while time.time() < deadline:
                chunk = second.recv(65536)
                if not chunk:
                    break
                drained += len(chunk)
            assert drained > 0                       # 确实转发了一些
            assert server.dropped >= 1               # 超限计入丢弃并断开
        finally:
            stop.set()
            t.join(2)
            first.close()
            second.close()
    finally:
        server.stop()


def test_relay_stop_is_clean():
    port = _free_port()
    server = RelayServer(_Settings(), host="127.0.0.1", port=port)
    server.start()
    _wait_ready(port)
    server.stop()
    assert server._thread is None
    # 停止后端口不再接受连接
    with pytest.raises(OSError):
        sock = socket.create_connection(("127.0.0.1", port), timeout=1)
        sock.close()
