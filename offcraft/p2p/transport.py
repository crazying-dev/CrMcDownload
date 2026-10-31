"""离线联机数据面传输：帧编解码 + 中继连接 + 端到端加密通道。

帧格式（与服务端中继解耦——中继只见不透明密文）::

    [u32 len][u32 channel][12B nonce][ciphertext + 16B tag]

``len`` 是其后（channel+nonce+ciphertext）的字节数，不含 ``len`` 自身 4 字节。
发送端按 ``MAX_PLAINTEXT`` 分片，保证单帧不超过 ``MAX_FRAME``。

Phase 1 只做「经服务端中继」的 TCP 传输：双方各主动连公网中继，天然穿 NAT。
UDP 打洞直连（省中继带宽）留待 Phase 1 后期 / Phase 2，这里保留 ``open_channel``
的统一入口签名，未实现时回落到中继。
"""

from __future__ import annotations

import select
import socket
import struct
import time
from typing import BinaryIO

from .crypto import CryptoChannel, NONCE_BYTES, P2PError, TAG_BYTES

__all__ = [
    "encode_frame",
    "decode_frame",
    "FrameReader",
    "SecureChannel",
    "RelayTransport",
    "MAX_PLAINTEXT",
    "MAX_FRAME",
    "MIN_FRAME",
]

_LEN_BYTES = 4
_CHANNEL_BYTES = 4
#: 单帧密文区上限：1 MiB（远超 MC 单个包，留足余量同时拒绝畸形超长）
MAX_FRAME = 1 * 1024 * 1024
#: 分片明文上限：留出 channel+nonce+tag 的开销
MAX_PLAINTEXT = MAX_FRAME - (_CHANNEL_BYTES + NONCE_BYTES + TAG_BYTES)
#: 空明文的合法最小帧
MIN_FRAME = _CHANNEL_BYTES + NONCE_BYTES + TAG_BYTES
#: select 轮询超时：既能在空闲时及时察觉对端关闭 / 套接字被拆，又不长时间硬阻塞
_POLL = 0.25


def encode_frame(channel: int, nonce: bytes, body: bytes) -> bytes:
    """把 ``(channel, nonce, 密文)`` 打成一个带长度前缀的帧。"""
    payload = channel.to_bytes(_CHANNEL_BYTES, "big") + nonce + body
    if len(payload) > MAX_FRAME:
        raise P2PError(f"帧超过上限 {MAX_FRAME} 字节")
    return struct.pack(">I", len(payload)) + payload


def decode_frame(payload: bytes) -> tuple[int, bytes, bytes]:
    """解析长度前缀之后的负载，返回 ``(channel, nonce, body)``。"""
    if len(payload) < MIN_FRAME:
        raise P2PError("帧过短（不完整或非法）")
    channel = int.from_bytes(payload[:_CHANNEL_BYTES], "big")
    nonce = payload[_CHANNEL_BYTES:_CHANNEL_BYTES + NONCE_BYTES]
    body = payload[_CHANNEL_BYTES + NONCE_BYTES:]
    return channel, nonce, body


class FrameReader:
    """从阻塞套接字按帧读取：``select`` + ``recv`` 可靠察觉对端关闭。

    Windows 上一个线程阻塞在 ``makefile().read()`` 时，另一侧 ``close`` 不一定能
    唤醒它；改用带超时的 ``select`` 轮询 + 原始 ``recv``，EOF/拆套接字都能在若有
    数据或连接断开的下一次轮询里干净返回。
    """

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._buf = bytearray()
        self._eof = False

    def _feed(self) -> bool | None:
        """拉取更多字节到缓冲。``True`` 有数据、``False`` EOF、``None`` 超时无数据。"""
        try:
            ready, _, _ = select.select([self._sock], [], [], _POLL)
        except (OSError, ValueError):
            self._eof = True
            return False
        if not ready:
            return None
        try:
            data = self._sock.recv(65536)
        except (OSError, ValueError):
            self._eof = True
            return False
        if not data:
            self._eof = True
            return False
        self._buf += data
        return True

    def _read_exact(self, count: int) -> bytes | None:
        while len(self._buf) < count:
            if self._feed() is False:
                if not self._buf:
                    return None
                raise P2PError("连接在帧中途被关闭（半包）")
        out = bytes(self._buf[:count])
        del self._buf[:count]
        return out

    def readline(self, timeout: float | None = None) -> bytes:
        """读一行（中继明文握手用）；EOF、超时或未找到换行时返回已缓冲内容。"""
        end = None if timeout is None else time.monotonic() + timeout
        while b"\n" not in self._buf and not self._eof:
            if end is not None and time.monotonic() > end:
                break
            self._feed()
        idx = self._buf.find(b"\n")
        if idx == -1:
            line = bytes(self._buf)
            self._buf.clear()
            return line
        line = bytes(self._buf[:idx + 1])
        del self._buf[:idx + 1]
        return line

    def read(self) -> tuple[int, bytes, bytes] | None:
        header = self._read_exact(_LEN_BYTES)
        if header is None:
            return None
        (length,) = struct.unpack(">I", header)
        if length < MIN_FRAME or length > MAX_FRAME:
            raise P2PError(f"非法帧长度 {length}")
        payload = self._read_exact(length)
        if payload is None:
            raise P2PError("连接在帧中途被关闭（半包）")
        return decode_frame(payload)

    def close(self) -> None:
        # 不拥有套接字（由 SecureChannel 统一关闭），仅标记 EOF。
        self._eof = True


class SecureChannel:
    """套接字之上的端到端加密通道：``send`` 明文字节，``recv`` 还原明文字节。"""

    def __init__(self, sock: socket.socket, crypto: CryptoChannel,
                 reader: FrameReader | None = None) -> None:
        self._sock = sock
        self._crypto = crypto
        self._reader = reader or FrameReader(sock)
        self._writer: BinaryIO = sock.makefile("wb")

    def send(self, data: bytes, channel: int = 0) -> None:
        if not data:
            return
        step = MAX_PLAINTEXT
        for offset in range(0, len(data), step):
            nonce, body = self._crypto.encrypt(data[offset:offset + step], channel)
            self._writer.write(encode_frame(channel, nonce, body))
        self._writer.flush()

    def recv(self) -> bytes | None:
        frame = self._reader.read()
        if frame is None:
            return None
        channel, nonce, body = frame
        return self._crypto.decrypt(nonce, body, channel)

    def close(self) -> None:
        # 不在此 shutdown 本端套接字：Windows 上对另一线程正阻塞 recv 的 socket
        # 调 shutdown 可能死锁。读侧靠 select 轮询超时自会察觉套接字被拆。
        for closer in (self._reader.close, self._writer.close):
            try:
                closer()
            except Exception:  # noqa: BLE001 - 关闭尽力而为
                pass
        try:
            self._sock.close()
        except OSError:
            pass


class RelayTransport:
    """连接镜像中继并用令牌成对，握手后交给 :class:`SecureChannel` 加密泵送。"""

    _HELLO_TIMEOUT = 20.0

    @staticmethod
    def connect(host: str, port: int, token: str, *, timeout: float = _HELLO_TIMEOUT) -> tuple[socket.socket, FrameReader]:
        """返回 ``(已配对套接字, 已读完握手行的 FrameReader)``；失败抛 :class:`P2PError`。"""
        try:
            sock = socket.create_connection((host, int(port)), timeout=timeout)
        except OSError as exc:
            raise P2PError(f"无法连接中继 {host}:{port}（{exc}）") from exc
        sock.settimeout(None)
        reader = FrameReader(sock)
        try:
            # 握手是唯一明文部分；readline 走 select 轮询，带整体超时防止中继悬挂
            sock.sendall(f"HELLO {token}\n".encode("ascii"))
            reply = reader.readline(timeout=timeout).strip()
            if not reply.startswith(b"OK"):
                reason = reply.decode("utf-8", "replace") or "中继无响应/拒绝"
                raise P2PError(f"中继配对失败：{reason}")
        except (OSError, P2PError):
            reader.close()
            try:
                sock.close()
            except OSError:
                pass
            raise
        return sock, reader
