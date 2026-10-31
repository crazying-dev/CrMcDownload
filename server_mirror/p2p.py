"""离线联机（P2P）：控制面信令 + 数据面中继。

设计（对应「离线版联机」计划 Phase 1）：

- ``SignalingRouter``：两层房间模型——**持久登记**（``p2p_rooms`` SQLite 表：全局唯一小写房间
  名 + 属主 device_id + 创建/列表/删除，每设备现存未删除房间数封顶 ``p2p_max_rooms_per_device``）
  与**实时会话**（内存房间 -> 成员名册 + 待配对中继令牌，随 TTL 空闲回收）。房主先 ``create``
  登记房间名（专用入口），再 ``open`` 开一个实时会话并登记从密码派生的 ``auth_key``；加入者 ``join``
  用它算出的 ``proof`` 由服务端校验——服务端拿不到密码，也拿不到能解密数据面的会话密钥，只能
  判定准入；房间未开放一律 404，**绝不自动建房**。加入者可查询 ``list`` 自己创建过的房间。房间规模封顶。
- ``RelayServer``：跑在独立线程的 asyncio 事件循环里的 TCP 中继，按令牌把两条
  入站连接成对拼桥。它只转发不透明密文帧、绝不解密；单会话累计字节 / 存活时长
  封顶后立刻断开，防止镜像沦为无限流量的开放中继。停止时干净关循环与套接字。

信令只走主 HTTP 端口的 ``/p2p/*``（控制面），中继监听独立 TCP 端口（数据面），
不经 nginx。凭证（auth_key / proof）与全局下载令牌相互独立。
"""

from __future__ import annotations

import asyncio
import hmac
import secrets
import threading
import time
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any

__all__ = ["SignalingRouter", "RelayServer", "PROOF_MESSAGE_PREFIX"]

# ------------------------------------------------------------------ 密码学原语
#: proof 覆盖的域分隔前缀，杜绝跨用途重放
PROOF_MESSAGE_PREFIX = b"offcraft-p2p-v1|"
_AUTH_KEY_BYTES = 32
_PUB_BYTES = 32
_PROOF_BYTES = 32          # HMAC-SHA256 摘要长度
_NONCE_MAX = 64
_MAX_TOKEN_BYTES = 64 * 1024


def _unhex(value: Any, want: int | None = None) -> bytes | None:
    if not isinstance(value, str):
        return None
    try:
        raw = bytes.fromhex(value)
    except (ValueError, TypeError):
        return None
    if want is not None and len(raw) != want:
        return None
    return raw


def proof_message(client_pub: bytes, nonce: bytes) -> bytes:
    return PROOF_MESSAGE_PREFIX + client_pub + nonce


def verify_proof(auth_key: bytes, client_pub_hex: Any, nonce_hex: Any, proof_hex: Any) -> bool:
    """校验加入者的准入 MAC：``proof = HMAC-SHA256(auth_key, PREFIX||pub||nonce)``。

    只用标准库（服务端不因离线联机新增二进制依赖）。任何一步长度不符或 MAC
    不匹配都返回假（不报错，交由信令层回 403）。时间安全比较。
    """
    pub = _unhex(client_pub_hex, _PUB_BYTES)
    nonce = _unhex(nonce_hex)
    proof = _unhex(proof_hex, _PROOF_BYTES)
    if pub is None or nonce is None or proof is None or not 1 <= len(nonce) <= _NONCE_MAX:
        return False
    expected = hmac.new(auth_key, proof_message(pub, nonce), sha256).digest()
    return hmac.compare_digest(expected, proof)


# ---------------------------------------------------------------------- 数据结构
@dataclass
class Peer:
    sid: str
    pub: str            # hex X25519 临时公钥
    addr: str           # 观测到的公网 "ip:port"
    world: str = ""
    port: int = 0
    last_seen: float = field(default_factory=time.time)

    def as_roster(self) -> dict[str, Any]:
        return {"pid": self.sid, "addr": self.addr, "world": self.world, "port": self.port}


@dataclass
class Room:
    code: str           # 规范化小写房间名（全局唯一，作实时会话字典键）
    auth_key: bytes     # host 登记的准入校验密钥（服务端不能据此解密）
    host_sid: str
    display: str = ""   # 原始大小写展示名
    owner: str = ""     # 创建者 device_id
    world: str = ""
    port: int = 0
    peers: dict[str, Peer] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    pending: dict[str, str] = field(default_factory=dict)  # sid -> 未领取的 relay token


def _client_addr(client: Any) -> str:
    """从 handler 传入的 client_address 提取 "ip:port"（缺失则 unknown）。"""
    if isinstance(client, (tuple, list)) and client:
        ip = str(client[0])
        port = str(client[1]) if len(client) > 1 else "0"
        return f"{ip}:{port}"
    return "unknown:0"


def _clamp_sid(value: Any) -> str:
    text = str(value or "")
    if 8 <= len(text) <= 64 and all(c in "0123456789abcdef" for c in text):
        return text
    return ""


def _norm_device(value: Any) -> str:
    """设备标识：客户端首启生成的稳定 hex；这里只做长度/字符集约束。"""
    text = str(value or "").strip()
    if 8 <= len(text) <= 64 and all(c in "0123456789abcdef" for c in text):
        return text
    return ""


# ------------------------------------------------------------------ 信令路由器
@dataclass
class SignalingRouter:
    settings: Any
    relay: "RelayServer | None" = None
    db: Any = None          # server_mirror.db.Database（房间持久登记）；None 时 create/open 不可用
    _rooms: dict[str, Room] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # 房间规模与配额
    _MAX_ROOMS_DEFAULT = 256
    _MAX_PEERS = 16
    _ROOM_TTL_DEFAULT = 180
    _MAX_PER_DEVICE_DEFAULT = 10

    @property
    def _ttl(self) -> float:
        return float(max(30, getattr(self.settings, "p2p_room_ttl", self._ROOM_TTL_DEFAULT)))

    @property
    def _max_rooms(self) -> int:
        return max(1, getattr(self.settings, "p2p_max_rooms", self._MAX_ROOMS_DEFAULT))

    @property
    def _max_per_device(self) -> int:
        return max(0, getattr(self.settings, "p2p_max_rooms_per_device", self._MAX_PER_DEVICE_DEFAULT))

    def relay_info(self) -> dict[str, Any]:
        host = (getattr(self.settings, "p2p_relay_host", "") or "").strip()
        host = host or getattr(self.settings, "host", "127.0.0.1")
        return {"host": host, "port": int(getattr(self.settings, "p2p_relay_port", 0) or 0)}

    def _prune(self, now: float) -> None:
        """回收空闲超时的房间（调用方持锁）。"""
        ttl = self._ttl
        for code in [c for c, r in self._rooms.items() if now - r.last_seen > ttl]:
            self._rooms.pop(code, None)

    def status(self) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            self._prune(now)
            return {
                "rooms": len(self._rooms),
                "peers": sum(len(r.peers) for r in self._rooms.values()),
            }

    # -------------------------------------------------------------- 分发入口
    def handle(
        self, path: str, payload: dict[str, Any], client: Any
    ) -> tuple[int, dict[str, Any]]:
        action = path[len("/p2p/"):].split("/", 1)[0]
        handler = {
            "create": self._create,
            "list": self._list,
            "delete": self._delete,
            "open": self._open,
            "join": self._join,
            "announce": self._announce,
            "relay": self._relay,
            "leave": self._leave,
        }.get(action)
        if handler is None:
            return 404, {"error": "未知信令接口"}
        name = str(payload.get("room") or "").strip()[:64]
        if action != "list" and not name:
            return 400, {"error": "缺少房间名"}
        return handler(name, payload, client)

    # ------------------------------------------------------------ /p2p/create
    def _create(self, name: str, payload: dict[str, Any], client: Any) -> tuple[int, dict]:
        """专用入口：登记一个新房间（全局唯一名 + 每设备配额）。密码/auth_key 不入 DB。"""
        device = _norm_device(payload.get("device_id"))
        if not device:
            return 400, {"error": "缺少设备标识"}
        if self.db is None:
            return 503, {"error": "房间存储未启用"}
        result = self.db.create_room(name, name, device, max_per_device=self._max_per_device)
        if result == "exists":
            return 409, {"error": "房间名已存在"}
        if result == "quota":
            return 403, {"error": f"每台设备最多 {self._max_per_device} 个房间"}
        return 200, {"ok": True, "name": name.strip().lower(),
                      "rooms": self.db.list_rooms(device)}

    # -------------------------------------------------------------- /p2p/list
    def _list(self, _name: str, payload: dict[str, Any], client: Any) -> tuple[int, dict]:
        device = _norm_device(payload.get("device_id"))
        if not device:
            return 400, {"error": "缺少设备标识"}
        rooms = self.db.list_rooms(device) if self.db is not None else []
        remaining = max(0, self._max_per_device - len(rooms))
        return 200, {"rooms": rooms, "remaining": remaining, "limit": self._max_per_device}

    # ------------------------------------------------------------ /p2p/delete
    def _delete(self, name: str, payload: dict[str, Any], client: Any) -> tuple[int, dict]:
        device = _norm_device(payload.get("device_id"))
        if not device:
            return 400, {"error": "缺少设备标识"}
        if self.db is None:
            return 503, {"error": "房间存储未启用"}
        key = name.strip().lower()
        reg = self.db.get_room(key)
        if reg is None:
            return 404, {"error": "房间不存在"}
        if reg.get("owner") != device:
            return 403, {"error": "只能删除自己创建的房间"}
        self.db.delete_room(key, device)
        with self._lock:
            self._rooms.pop(key, None)  # 若正开放，一并撤下实时会话
        return 200, {"ok": True}

    # -------------------------------------------------------------- /p2p/open
    def _open(self, name: str, payload: dict[str, Any], client: Any) -> tuple[int, dict]:
        """房主开一个实时会话供加入：房间须已由本设备创建登记。"""
        device = _norm_device(payload.get("device_id"))
        if not device:
            return 400, {"error": "缺少设备标识"}
        key = name.strip().lower()
        reg = self.db.get_room(key) if self.db is not None else None
        if reg is None:
            return 404, {"error": "房间未创建，请先创建"}
        if reg.get("owner") != device:
            return 403, {"error": "不是你的房间"}
        auth_key = _unhex(payload.get("auth_key"), _AUTH_KEY_BYTES)
        if auth_key is None:
            return 400, {"error": "缺少或非法的 auth_key"}
        pub_hex = payload.get("client_pub")
        addr = _client_addr(client)
        now = time.time()
        with self._lock:
            self._prune(now)
            if key in self._rooms and self._rooms[key].host_sid:
                return 409, {"error": "该房间已开放"}
            if len(self._rooms) >= self._max_rooms:
                return 503, {"error": "房间数量已达上限"}
            sid = secrets.token_hex(16)
            room = Room(
                code=key,
                auth_key=auth_key,
                host_sid=sid,
                display=name.strip()[:64],
                owner=device,
                world=str(payload.get("world") or "")[:64],
                port=_as_port(payload.get("port")),
                last_seen=now,
            )
            room.peers[sid] = Peer(sid=sid, pub=_norm_pub(pub_hex), addr=addr,
                                   world=room.world, port=room.port, last_seen=now)
            self._rooms[key] = room
        if self.db is not None:
            self.db.touch_open(key)
        return 200, {
            "sid": sid,
            "host": room.host_sid,
            "peers": [p.as_roster() for p in room.peers.values()],
            "relay": self.relay_info(),
        }

    # -------------------------------------------------------------- /p2p/join
    def _join(self, name: str, payload: dict[str, Any], client: Any) -> tuple[int, dict]:
        """加入者凭 proof 进入已开放的会话；房间不存在或未开放一律报错，绝不自动建房。"""
        key = name.strip().lower()
        pub_hex = payload.get("client_pub")
        nonce_hex = payload.get("nonce")
        proof_hex = payload.get("proof")
        addr = _client_addr(client)
        now = time.time()
        with self._lock:
            self._prune(now)
            room = self._rooms.get(key)
            if room is None:
                return 404, {"error": "房间未开放或已过期"}
            if not verify_proof(room.auth_key, pub_hex, nonce_hex, proof_hex):
                return 403, {"error": "准入校验失败（密码或房间名不符）"}
            if len(room.peers) >= self._MAX_PEERS:
                return 503, {"error": "房间人数已满"}
            sid = secrets.token_hex(16)
            room.peers[sid] = Peer(sid=sid, pub=_norm_pub(pub_hex), addr=addr, last_seen=now)
            room.last_seen = now
            return 200, {
                "sid": sid,
                "host": room.host_sid,
                "peers": [p.as_roster() for p in room.peers.values()],
                "relay": self.relay_info(),
            }

    # ---------------------------------------------------------- /p2p/announce
    def _announce(self, code: str, payload: dict[str, Any], client: Any) -> tuple[int, dict]:
        code = code.strip().lower()
        sid = _clamp_sid(payload.get("sid"))
        if not sid:
            return 400, {"error": "缺少会话标识"}
        addr = _client_addr(client)
        now = time.time()
        with self._lock:
            self._prune(now)
            room = self._rooms.get(code)
            if room is None:
                return 404, {"error": "房间未开启或已过期"}
            peer = room.peers.get(sid)
            if peer is None:
                return 403, {"error": "不属于该房间"}
            peer.addr = addr
            peer.last_seen = now
            if str(payload.get("world") or "") and room.host_sid == sid:
                room.world = str(payload["world"])[:64]
                room.port = _as_port(payload.get("port"))
                peer.world, peer.port = room.world, room.port
            room.last_seen = now
            pending = room.pending.pop(sid, "")
            return 200, {"ok": True, "relay_token": pending}

    # ------------------------------------------------------------- /p2p/relay
    def _relay(self, code: str, payload: dict[str, Any], client: Any) -> tuple[int, dict]:
        code = code.strip().lower()
        sid = _clamp_sid(payload.get("sid"))
        target = _clamp_sid(payload.get("peer"))
        if not sid or not target:
            return 400, {"error": "缺少 sid 或 peer"}
        now = time.time()
        with self._lock:
            self._prune(now)
            room = self._rooms.get(code)
            if room is None:
                return 404, {"error": "房间未开启或已过期"}
            if sid not in room.peers or target not in room.peers:
                return 403, {"error": "请求方或目标不在房间内"}
            if self.relay is None:
                return 503, {"error": "中继未启用"}
            token = self.relay.new_token(code, requester=sid, target=target)
            room.pending[target] = token
            room.last_seen = now
            return 200, {"token": token, "relay": self.relay_info()}

    # ------------------------------------------------------------- /p2p/leave
    def _leave(self, code: str, payload: dict[str, Any], client: Any) -> tuple[int, dict]:
        code = code.strip().lower()
        sid = _clamp_sid(payload.get("sid"))
        now = time.time()
        with self._lock:
            self._prune(now)
            room = self._rooms.get(code)
            if room is not None and sid:
                room.peers.pop(sid, None)
                for t in [t for t, tgt in _room_pending(room) if tgt == sid]:
                    room.pending.pop(t, None)
                if not room.peers or room.host_sid == sid:
                    self._rooms.pop(code, None)
            return 200, {"ok": True}


def _room_pending(room: Room) -> dict[str, str]:
    return dict(room.pending)


def _norm_pub(value: Any) -> str:
    raw = _unhex(value, _PUB_BYTES)
    return value if raw is not None else ""


def _as_port(value: Any) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return 0
    return port if 0 <= port <= 65535 else 0


# ------------------------------------------------------------------ 中继服务
@dataclass
class _Token:
    token: str
    room: str
    requester: str
    target: str
    expires: float


class RelayServer:
    """基于 asyncio 的 TCP 中继：按令牌成对拼桥两条入站连接。

    握手是唯一的明文部分：``HELLO <base64url token>\\n`` -> ``OK\\n``；之后双向
    原样转发不透明密文帧。每会话累计字节与存活时长封顶即断开。
    """

    _HANDSHAKE_TIMEOUT = 10.0
    _BRIDGE_IDLE = 120.0

    def __init__(
        self,
        settings: Any,
        *,
        host: str = "0.0.0.0",
        port: int | None = None,
        bytes_cap: int | None = None,
        token_ttl: float | None = None,
    ) -> None:
        self.host = host
        self.port = int(port if port is not None else getattr(settings, "p2p_relay_port", 0) or 0)
        self.bytes_cap = int(
            bytes_cap if bytes_cap is not None else getattr(settings, "p2p_relay_bytes_cap", 0) or 0
        )
        self.token_ttl = float(token_ttl if token_ttl is not None
                               else max(60, getattr(settings, "p2p_room_ttl", 180)))
        self._tokens: dict[str, _Token] = {}
        self._pairs: dict[str, dict[str, Any]] = {}
        self._guard = threading.Lock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.AbstractServer | None = None
        self._stop = threading.Event()
        self.bridged = 0
        self.dropped = 0

    # ------------------------------------------------------------ 令牌生命周期
    def _prune(self, now: float) -> None:
        for t in [k for k, v in self._tokens.items() if v.expires < now]:
            self._tokens.pop(t, None)

    def new_token(self, room: str, *, requester: str, target: str) -> str:
        now = time.time()
        token = secrets.token_urlsafe(24)[:_MAX_TOKEN_BYTES]
        with self._guard:
            self._prune(now)
            self._tokens[token] = _Token(
                token=token, room=room, requester=requester, target=target,
                expires=now + self.token_ttl,
            )
        return token

    def _peek_token(self, token: str) -> _Token | None:
        """查询配对令牌是否有效（未过期）。取走由桥接流程负责。"""
        now = time.time()
        with self._guard:
            self._prune(now)
            return self._tokens.get(token)

    # -------------------------------------------------------------- 线程生命周期
    def start(self) -> None:
        if self._thread is not None or self.port <= 0:
            return
        self._thread = threading.Thread(target=self._run, name="p2p-relay", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 6.0) -> None:
        self._stop.set()
        loop = self._loop
        server = self._server
        if loop is not None and server is not None:
            # 优雅关监听套接字：serve_forever 会因此正常返回，避免在 Windows
            # proactor 上直接 loop.stop() 引发的 GeneratorExit / 端口未干净释放。
            try:
                loop.call_soon_threadsafe(server.close)
            except RuntimeError:
                pass
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve())
        except RuntimeError:
            pass
        finally:
            try:
                # 取消尚在挂起的中继会话（先到者等配对 / 桥接泵送），再关循环。
                pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
            except RuntimeError:
                pass
            loop.close()
            self._loop = None
            self._server = None

    async def _serve(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        async with self._server:
            try:
                await self._server.serve_forever()
            except asyncio.CancelledError:
                # stop() 调 server.close() 会取消 serve_forever：视作正常停机。
                pass

    # ------------------------------------------------------------------ 连接处理
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            hello = await asyncio.wait_for(reader.readline(), self._HANDSHAKE_TIMEOUT)
        except (asyncio.TimeoutError, ConnectionError, ValueError):
            await self._fail(writer, "bad-handshake")
            return
        token = _parse_hello(hello)
        if not token or len(token) > _MAX_TOKEN_BYTES:
            await self._fail(writer, "bad-handshake")
            return
        record = self._peek_token(token)
        if record is None:
            await self._fail(writer, "unknown-token")
            return
        existing = self._pairs.pop(token, None)
        if existing is None:
            # 先到的一侧：登记自己后挂起，等后到者取走并桥接完成。令牌暂留
            # _tokens（凭 TTL 兜底回收），好让后到者仍能通过 _peek_token 校验。
            done = asyncio.Event()
            self._pairs[token] = {"reader": reader, "writer": writer, "done": done}
            try:
                await asyncio.wait_for(done.wait(), self._BRIDGE_IDLE)
            except asyncio.TimeoutError:
                pass
            finally:
                # 配对未成就到期：撤登记与令牌，避免令牌被后续连接重复利用。
                if self._pairs.get(token) and self._pairs[token].get("done") is done:
                    self._pairs.pop(token, None)
                self._tokens.pop(token, None)
                self._close(writer)
            return
        # 后到的一侧：此刻消费令牌（一次性），取先到者成对，双方各回 OK，随后双向泵送
        self._tokens.pop(token, None)
        await self._pair_and_bridge(reader, writer, existing)

    async def _pair_and_bridge(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, first: dict[str, Any]
    ) -> None:
        first_reader: asyncio.StreamReader = first["reader"]
        first_writer: asyncio.StreamWriter = first["writer"]
        for side in (writer, first_writer):
            try:
                side.write(b"OK\n")
                await side.drain()
            except (ConnectionError, OSError):
                pass
        self.bridged += 1
        try:
            await self._bridge(reader, writer, first_reader, first_writer)
        finally:
            first["done"].set()

    async def _bridge(self, r1, w1, r2, w2) -> None:
        cap = self.bytes_cap or (1 << 62)
        counter = [0]

        async def pipe(src, dst):
            while True:
                data = await src.read(65536)
                if not data:
                    break
                counter[0] += len(data)
                dst.write(data)
                try:
                    await dst.drain()
                except (ConnectionError, OSError):
                    break
                if cap and counter[0] > cap:
                    self.dropped += 1
                    break

        t1 = asyncio.ensure_future(pipe(r1, w2))
        t2 = asyncio.ensure_future(pipe(r2, w1))
        try:
            await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED,
                               timeout=self._BRIDGE_IDLE)
        finally:
            for t in (t1, t2):
                if not t.done():
                    t.cancel()
            self._close(w1)
            self._close(w2)

    @staticmethod
    def _close(writer) -> None:
        try:
            if not writer.is_closing():
                writer.close()
        except (OSError, ConnectionError):
            pass

    async def _fail(self, writer, reason: str) -> None:
        self.dropped += 1
        try:
            writer.write(f"ERR {reason}\n".encode("ascii"))
            await writer.drain()
        except (OSError, ConnectionError):
            pass
        self._close(writer)


def _parse_hello(hello: bytes) -> str:
    if not hello.startswith(b"HELLO "):
        return ""
    body = hello[6:].strip()
    try:
        return body.decode("ascii")
    except UnicodeDecodeError:
        return ""
