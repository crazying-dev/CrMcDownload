"""后台服务器探测：asyncio 批量 SLP，写入库并支撑热度榜。

设计要点：

- 用标准库 ``asyncio.open_connection`` 而不是 aiohttp —— Server List Ping 是裸
  TCP 协议，aiohttp 只处理 HTTP，硬套反而更绕；asyncio 自带并发 + 超时 + 连接复用。
- 探测跑在独立线程的事件循环里（与 ``ThreadingHTTPServer`` 并存），停止时
  ``loop.call_soon_threadsafe(loop.stop)`` + 线程 join，保证干净退出。
- 目标地址同样过 SSRF 校验（默认拒绝私网 / 回环），只有显式 ``allow_private``
  才放开（供本机测试与内网榜单）。
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from typing import Any, Iterable

from server_mirror.ssrf_guard import resolve_is_public

__all__ = ["ServerProbe", "parse_slp_response", "probe_once", "encode_varint"]

_MAX_JSON_BYTES = 512 * 1024
_COLOR = re.compile(r"§[0-9a-fk-orA-FK-OR]")
_HEX_COLOR = re.compile(r"&[0-9a-fk-orA-FK-OR]")


# --------------------------------------------------------------- 协议编解码
def encode_varint(value: int) -> bytes:
    if value < 0:
        value &= 0xFFFFFFFF
    out = bytearray()
    for index in range(5):
        chunk = value & 0x7F
        value >>= 7
        out.append(chunk | 0x80 if value else chunk)
        if not value:
            break
    return bytes(out)


def _varint_string(text: str) -> bytes:
    payload = text.encode("utf-8")
    return encode_varint(len(payload)) + payload


def build_request(host: str, port: int, name: str = "OffCraft-Mirror") -> bytes:
    """Handshake(nextState=1) + Status Request。

    状态阶段只能发 ``packetId=0x00`` 的空包；``name`` 仅为兼容旧调用保留，
    发 Login Start 会被 Velocity / BungeeCord 等严格服务端直接断开。
    """
    body = encode_varint(0) + encode_varint(-1) + _varint_string(host) + int(port).to_bytes(2, "big") + encode_varint(1)
    handshake = encode_varint(len(body)) + body
    return handshake + b"\x01\x00"


def _decode_varint(data: bytes) -> tuple[int, int]:
    """从 bytes 头部解一个 VarInt，返回 ``(值, 消耗字节数)``。"""
    value = 0
    for index in range(5):
        if index >= len(data):
            raise ValueError("VarInt 数据不完整")
        chunk = data[index]
        value |= (chunk & 0x7F) << (7 * index)
        if not chunk & 0x80:
            return value, index + 1
    raise ValueError("VarInt 超过 5 字节")


async def _read_varint(reader: asyncio.StreamReader) -> int:
    value = 0
    for index in range(5):
        byte = await reader.readexactly(1)
        chunk = byte[0]
        value |= (chunk & 0x7F) << (7 * index)
        if not chunk & 0x80:
            return value
    raise ValueError("VarInt 超过 5 字节")


# ------------------------------------------------------------------- 解析
def strip_color_codes(text: str) -> str:
    return _COLOR.sub("", _HEX_COLOR.sub("", str(text or "")))


def _chat_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_chat_to_text(item) for item in value)
    if isinstance(value, dict):
        text = str(value.get("text") or "")
        for key in ("extra", "with"):
            if value.get(key) is not None:
                text += _chat_to_text(value[key])
        return text
    return str(value)


def _favicon_bytes(value: Any) -> bytes | None:
    import base64

    text = str(value or "")
    if not text.startswith("data:image/"):
        return None
    _, _, payload = text.partition(",")
    if not payload:
        return None
    try:
        return base64.b64decode(payload.encode("ascii"))[:2 * 1024 * 1024]
    except Exception:  # noqa: BLE001
        return None


def parse_slp_response(data: dict[str, Any]) -> dict[str, Any]:
    version = data.get("version") or {}
    players = data.get("players") or {}
    sample = [
        {"name": str(item.get("name") or ""), "uuid": str(item.get("id") or item.get("uuid") or "")}
        for item in (players.get("sample") or [])
        if isinstance(item, dict) and item.get("name")
    ]
    return {
        "motd": strip_color_codes(_chat_to_text(data.get("description"))).strip("\n")[:512],
        "version": str(version.get("name") or "")[:120],
        "online": int(_as_int(players.get("online")) or 0),
        "max": int(_as_int(players.get("max")) or 0),
        "players": sample,
        "favicon": _favicon_bytes(data.get("favicon")),
    }


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------- 探测
async def probe_once(
    host: str,
    port: int = 25565,
    *,
    timeout: float = 5.0,
    name: str = "OffCraft-Mirror",
    allow_private: bool = False,
) -> dict[str, Any]:
    """探测一台服务器，返回入库所需的规整字段（``ok`` 为假时带中文 ``error``）。"""
    host = str(host or "").strip().lower()
    if not allow_private:
        allowed, reason = await asyncio.get_running_loop().run_in_executor(None, resolve_is_public, host)
        if not allowed:
            return {"host": host, "port": port, "ok": False, "error": reason, "ts": time.time()}
    started = time.perf_counter()
    result: dict[str, Any] = {"host": host, "port": port, "ok": False, "error": "", "ts": time.time()}
    reader: asyncio.StreamReader | None = None
    writer: asyncio.StreamWriter | None = None
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        writer.write(build_request(host, port, name))
        await writer.drain()
        length = await asyncio.wait_for(_read_varint(reader), timeout)
        if length <= 0:
            result["error"] = "服务器返回空响应"
            return result
        if length > _MAX_JSON_BYTES:
            result["error"] = "服务器响应过大"
            return result
        frame = await asyncio.wait_for(reader.readexactly(length), timeout)
        # 帧格式：packetId(0x00) + 字符串长度 VarInt + JSON
        packet_id, offset = _decode_varint(frame)
        if packet_id != 0x00:
            result["error"] = f"响应包 id 异常（0x{packet_id:02x}）"
            return result
        size, step = _decode_varint(frame[offset:])
        offset += step
        if size <= 0 or len(frame) < offset + size:
            result["error"] = "响应 JSON 长度不完整"
            return result
        payload = frame[offset : offset + size]
        latency_ms = (time.perf_counter() - started) * 1000.0
        data = json.loads(payload.decode("utf-8", "replace"))
        if not isinstance(data, dict) or "version" not in data:
            result["error"] = "响应缺少 version 字段"
            return result
        parsed = parse_slp_response(data)
        result.update(parsed)
        result["ok"] = True
        result["latency_ms"] = round(latency_ms, 1)
        return result
    except asyncio.TimeoutError:
        result["error"] = "连接超时"
        return result
    except asyncio.IncompleteReadError:
        result["error"] = "连接被对方关闭（响应不完整）"
        return result
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        result["error"] = "不是 Minecraft 服务器"
        return result
    except OSError as exc:
        result["error"] = f"无法连接：{exc.strerror or exc}"
        return result
    finally:
        if writer is not None:
            try:
                writer.close()
            except OSError:
                pass


class ServerProbe:
    """按固定间隔探测 ``enabled`` 服务器的后台线程。"""

    def __init__(
        self,
        db,
        *,
        interval: float = 300.0,
        concurrency: int = 8,
        timeout: float = 5.0,
        allow_private: bool = False,
        popular_path=None,
    ) -> None:
        self.db = db
        self.interval = max(15.0, float(interval))
        self.concurrency = max(1, int(concurrency))
        self.timeout = float(timeout)
        self.allow_private = allow_private
        self.popular_path = popular_path
        self.last_cycle_at: float | None = None
        self.cycle_count = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def seed_if_empty(self) -> int:
        """榜单为空时导入内置热门种子（人工维护的公开服务器）。"""
        if self.db.count_servers() > 0 or not self.popular_path:
            return 0
        try:
            data = json.loads(self.popular_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        items = data.get("servers") if isinstance(data, dict) else data
        count = 0
        for item in items or []:
            host = str((item or {}).get("host") or "").strip().lower()
            if not host:
                continue
            self.db.upsert_server(
                host,
                int(item.get("port") or 25565),
                label=str(item.get("label") or ""),
                source="builtin",
            )
            count += 1
        return count

    def start(self) -> None:
        if self._thread is not None:
            return
        self.seed_if_empty()
        self._thread = threading.Thread(target=self._run, name="server-probe", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 6.0) -> None:
        self._stop.set()
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._cycle_loop())
        except RuntimeError:
            # loop.stop() 会让 run_until_complete 抛 "Event loop stopped"，属正常退出
            pass
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except RuntimeError:
                pass
            loop.close()
            self._loop = None

    async def _cycle_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_cycle()
            except Exception:  # noqa: BLE001 - 单轮异常不能拖垮线程
                pass
            self._stop.wait(self.interval)

    async def run_cycle(self, targets: Iterable[tuple[str, int]] | None = None) -> int:
        """探测一轮；``targets`` 为空时取库里所有 enabled 服务器。返回探测数。"""
        if targets is None:
            targets = [(row["host"], row["port"]) for row in self.db.list_enabled()]
        targets = list(targets)
        if not targets:
            self.last_cycle_at = time.time()
            return 0
        semaphore = asyncio.Semaphore(self.concurrency)

        async def worker(host: str, port: int) -> None:
            async with semaphore:
                if self._stop.is_set():
                    return
                await self.probe_and_record(host, port)

        await asyncio.gather(*(worker(h, p) for h, p in targets))
        self.last_cycle_at = time.time()
        self.cycle_count += 1
        return len(targets)

    async def probe_and_record(self, host: str, port: int) -> dict[str, Any]:
        result = await probe_once(host, port, timeout=self.timeout, allow_private=self.allow_private)
        self.db.record_ping(
            host,
            port,
            ok=result.get("ok", False),
            ts=result.get("ts"),
            latency_ms=result.get("latency_ms"),
            online=result.get("online"),
            players_max=result.get("max"),
            version=result.get("version", ""),
            motd=result.get("motd", ""),
            players=result.get("players"),
            favicon=result.get("favicon"),
            error=result.get("error", ""),
        )
        return result

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self._thread is not None,
            "interval": self.interval,
            "concurrency": self.concurrency,
            "last_cycle_at": self.last_cycle_at,
            "cycle_count": self.cycle_count,
        }
