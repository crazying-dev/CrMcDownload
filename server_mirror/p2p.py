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
- ``UdpEdgeServer``：UDP 数据面的「边缘服务」——登记每台设备的公网 UDP 端点
  （``HELLO <token>``），使房主与加入者能从名册拿到对端地址做打洞；打洞失败时按
  ``R <dst_sid>`` 前缀转发密文。它同样只转发不透明明文（实际是 AEAD 密文）。

信令只走主 HTTP 端口的 ``/p2p/*``（控制面），中继监听独立 TCP 端口 / UDP 边缘端口
（数据面），不经 nginx。凭证（auth_key / proof / edge_token）与全局下载令牌相互独立。
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

__all__ = [
    "SignalingRouter",
    "RelayServer",
    "UdpEdgeServer",
    "EdgeTokenStore",
    "PROOF_MESSAGE_PREFIX",
    "VIP_PREFIX",
    "VIP_HOST",
    "derive_peer_vip",
]

# ------------------------------------------------------------------ 密码学原语
#: proof 覆盖的域分隔前缀，杜绝跨用途重放
PROOF_MESSAGE_PREFIX = b"offcraft-p2p-v1|"
_AUTH_KEY_BYTES = 32
_PUB_BYTES = 32
_PROOF_BYTES = 32          # HMAC-SHA256 摘要长度
_NONCE_MAX = 64
_MAX_TOKEN_BYTES = 64 * 1024

#: 虚拟局域网网段：房主固定 ``10.66.0.1``，加入者派生 ``10.66.0.2`` - ``10.66.0.254``
VIP_PREFIX = "10.66.0"
VIP_HOST = f"{VIP_PREFIX}.1"
VIP_FIRST = 2
VIP_LAST = 254


def derive_peer_vip(auth_key: bytes, device: str) -> str:
    """按房间准入密钥 + 设备标识派生稳定虚拟 IP。

    ``HMAC-SHA256(auth_key, DOMAIN||device)`` 取前两字节映射进 ``.2`` - ``.254``；
    同一设备在同一房间（同一 auth_key）重连拿回同一 IP。服务端另有线性探测兜底冲突。
    """
    digest = hmac.new(auth_key, b"offcraft-vip-v1|" + str(device).encode("utf-8"), sha256).digest()
    span = VIP_LAST - VIP_FIRST + 1
    return f"{VIP_PREFIX}.{VIP_FIRST + int.from_bytes(digest[:2], 'big') % span}"


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
    device: str = ""    # 稳定设备标识：踢出后据此阻止同设备换 sid 重入
    vip: str = ""       # 虚拟 IP（10.66.0.x）；vpn 关闭时为空
    udp: str = ""       # UDP 边缘登记的观测端点 "ip:port"；未登记为空
    edge_token: str = ""  # 本端边缘令牌（仅随本端响应下发，不进名册）
    last_seen: float = field(default_factory=time.time)

    def as_roster(self) -> dict[str, Any]:
        return {
            "pid": self.sid,
            "addr": self.addr,
            "world": self.world,
            "port": self.port,
            "vip": self.vip,
            "udp": self.udp,
        }


@dataclass
class Room:
    code: str           # 规范化小写房间名（全局唯一，作实时会话字典键）
    auth_key: bytes     # host 登记的准入校验密钥（服务端不能据此解密）
    host_sid: str
    display: str = ""   # 原始大小写展示名
    owner: str = ""     # 创建者 device_id
    world: str = ""
    port: int = 0
    #: 房主控制面口令：open 时随机生成、仅回给房主、不进名册；kick 用它鉴权（服务端强制「仅房主」）
    host_secret: str = ""
    peers: dict[str, Peer] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    pending: dict[str, str] = field(default_factory=dict)  # sid -> 未领取的 relay token
    #: 被踢出的 sid：拒绝其再用旧 sid 申请在途配对令牌
    blocked: set[str] = field(default_factory=set)
    #: 被踢出的设备：同一设备换新 sid 也不能重新加入（改密码/重开房间时清空）
    blocked_devices: set[str] = field(default_factory=set)
    #: 设备 -> 虚拟 IP 的保留映射：成员离开后不回收，保证同一设备重连拿回同一 IP
    vip_by_device: dict[str, str] = field(default_factory=dict)


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
    edge: "UdpEdgeServer | None" = None   # UDP 边缘服务；None 时不下发 edge_token/udp
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

    @property
    def _vpn(self) -> bool:
        """虚拟局域网（VIP + UDP 边缘）是否启用；关闭后响应不含 vip/edge_token。"""
        return bool(getattr(self.settings, "p2p_vpn_enable", True))

    def relay_info(self) -> dict[str, Any]:
        host = (getattr(self.settings, "p2p_relay_host", "") or "").strip()
        host = host or getattr(self.settings, "host", "127.0.0.1")
        return {"host": host, "port": int(getattr(self.settings, "p2p_relay_port", 0) or 0)}

    def edge_info(self) -> dict[str, Any]:
        """UDP 边缘端口对外通告地址（与中继共用对外主机名）。"""
        host = (getattr(self.settings, "p2p_relay_host", "") or "").strip()
        host = host or getattr(self.settings, "host", "127.0.0.1")
        return {"host": host, "port": int(getattr(self.settings, "p2p_udp_port", 0) or 0)}

    def _allocate_vip(self, room: Room, device: str, *, host: bool) -> str:
        """分配虚拟 IP（调用方持锁）：房主固定 ``.1``；加入者按设备派生，冲突线性探测。

        ``vip_by_device`` 保留历史分配，成员离开后不回收，保证同一设备重连拿回同一 IP。
        """
        if not self._vpn:
            return ""
        if host:
            if device:
                room.vip_by_device[device] = VIP_HOST
            return VIP_HOST
        if device and device in room.vip_by_device:
            return room.vip_by_device[device]
        used = set(room.vip_by_device.values())
        used.update(p.vip for p in room.peers.values() if p.vip)
        span = VIP_LAST - VIP_FIRST + 1
        start = VIP_FIRST
        if device:
            start = int(derive_peer_vip(room.auth_key, device).rsplit(".", 1)[1])
        for step in range(span):
            number = VIP_FIRST + ((start - VIP_FIRST + step) % span)
            ip = f"{VIP_PREFIX}.{number}"
            if ip not in used:
                if device:
                    room.vip_by_device[device] = ip
                return ip
        return ""   # 253 个地址耗尽（房间人数封顶 16，正常不会发生）

    def _issue_edge(self, room: Room, sid: str) -> str:
        """签发/复用本端边缘令牌（调用方持锁）：绑定 room+sid+device+vip，TTL 到期后重发。"""
        if not self._vpn or self.edge is None:
            return ""
        peer = room.peers.get(sid)
        if peer is None:
            return ""
        token = peer.edge_token
        if token and self.edge.token_valid(token):
            return token
        token = self.edge.issue_token(
            room=room.code, sid=sid, device=peer.device, vip=peer.vip,
        )
        peer.edge_token = token
        return token

    def _roster_payload(self, room: Room) -> list[dict[str, Any]]:
        """名册条目（调用方持锁）：顺带用 UDP 边缘的实时登记刷新每人的 ``udp`` 端点。"""
        for peer in room.peers.values():
            peer.udp = self.edge.endpoint(room.code, peer.sid) if self.edge is not None else ""
        return [p.as_roster() for p in room.peers.values()]

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
            "roster": self._roster,
            "kick": self._kick,
            "rekey": self._rekey,
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
        if self.edge is not None:
            self.edge.evict_room(key)
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
                host_secret=secrets.token_hex(32),
                last_seen=now,
            )
            host_vip = self._allocate_vip(room, device, host=True)
            room.peers[sid] = Peer(sid=sid, pub=_norm_pub(pub_hex), addr=addr,
                                   world=room.world, port=room.port, device=device,
                                   vip=host_vip, last_seen=now)
            self._rooms[key] = room
            edge_token = self._issue_edge(room, sid)
            roster = self._roster_payload(room)
        if self.db is not None:
            self.db.touch_open(key)
        response = {
            "sid": sid,
            "host": room.host_sid,
            "host_secret": room.host_secret,   # 仅回给房主，用于 kick 鉴权；不进名册
            "peers": roster,
            "relay": self.relay_info(),
        }
        if self._vpn:
            response["vip"] = host_vip
            response["host_vip"] = host_vip
            if self.edge is not None:
                response["edge"] = self.edge_info()
                if edge_token:
                    response["edge_token"] = edge_token
        return 200, response

    # -------------------------------------------------------------- /p2p/join
    def _join(self, name: str, payload: dict[str, Any], client: Any) -> tuple[int, dict]:
        """加入者凭 proof 进入已开放的会话；房间不存在或未开放一律报错，绝不自动建房。"""
        key = name.strip().lower()
        pub_hex = payload.get("client_pub")
        nonce_hex = payload.get("nonce")
        proof_hex = payload.get("proof")
        device = _norm_device(payload.get("device_id"))
        addr = _client_addr(client)
        now = time.time()
        with self._lock:
            self._prune(now)
            room = self._rooms.get(key)
            if room is None:
                return 404, {"error": "房间未开放或已过期"}
            if not verify_proof(room.auth_key, pub_hex, nonce_hex, proof_hex):
                return 403, {"error": "准入校验失败（密码或房间名不符）"}
            if device and (device in room.blocked_devices
                           or (payload.get("sid") and _clamp_sid(payload.get("sid")) in room.blocked)):
                return 403, {"error": "你已被房主移出该房间，无法重新加入"}
            if len(room.peers) >= self._MAX_PEERS:
                return 503, {"error": "房间人数已满"}
            sid = secrets.token_hex(16)
            vip = self._allocate_vip(room, device, host=False)
            room.peers[sid] = Peer(sid=sid, pub=_norm_pub(pub_hex), addr=addr,
                                   device=device, vip=vip, last_seen=now)
            room.last_seen = now
            host_peer = room.peers.get(room.host_sid)
            host_vip = host_peer.vip if host_peer is not None else ""
            edge_token = self._issue_edge(room, sid)
            roster = self._roster_payload(room)
        response = {
            "sid": sid,
            "host": room.host_sid,
            "peers": roster,
            "relay": self.relay_info(),
        }
        if self._vpn:
            response["vip"] = vip
            response["host_vip"] = host_vip
            if self.edge is not None:
                response["edge"] = self.edge_info()
                if edge_token:
                    response["edge_token"] = edge_token
        return 200, response

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
            edge_token = self._issue_edge(room, sid)
            response = {
                "ok": True,
                "relay_token": pending,
                "peers": self._roster_payload(room),
            }
            if self._vpn:
                response["vip"] = peer.vip
                if edge_token:
                    response["edge_token"] = edge_token
                if self.edge is not None:
                    response["edge"] = self.edge_info()
        return 200, response

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
            if sid in room.blocked or target in room.blocked:
                return 403, {"error": "该成员已被移出房间"}
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
        host_left = False
        with self._lock:
            self._prune(now)
            room = self._rooms.get(code)
            if room is not None and sid:
                # 虚拟 IP 保留在 room.vip_by_device：同一设备重连拿回同一 IP
                room.peers.pop(sid, None)
                for t in [t for t, tgt in _room_pending(room) if tgt == sid]:
                    room.pending.pop(t, None)
                if not room.peers or room.host_sid == sid:
                    self._rooms.pop(code, None)
                    host_left = True
        if self.edge is not None:
            if host_left:
                self.edge.evict_room(code)
            elif sid:
                self.edge.evict(code, sid)
        return 200, {"ok": True}

    # ------------------------------------------------------------- /p2p/roster
    def _roster(self, name: str, payload: dict[str, Any], client: Any) -> tuple[int, dict]:
        """当前会话在线名册：任何在房成员（sid∈peers）都可查（只读）。

        名册条目含 pid/addr/world/port/vip/udp——不暴露中继地址以外的服务端域名；addr 为
        对端观测地址（供打洞），vip 为虚拟 IP，udp 为对端在 UDP 边缘登记的公网端点（未登记为空）。
        """
        key = name.strip().lower()
        sid = _clamp_sid(payload.get("sid"))
        now = time.time()
        with self._lock:
            self._prune(now)
            room = self._rooms.get(key)
            if room is None:
                return 404, {"error": "房间未开放或已过期"}
            if not sid or sid not in room.peers:
                return 403, {"error": "不属于该房间"}
            room.last_seen = now
            response: dict[str, Any] = {"peers": self._roster_payload(room)}
            if self._vpn:
                # 名册轮询同时刷新本端边缘令牌：令牌 TTL 短于会话时长，vpnd 每 60s
                # 发一次 HELLO，必须能在令牌过期后拿到新令牌（只发给令牌持有者本人）。
                peer = room.peers[sid]
                response["vip"] = peer.vip
                edge_token = self._issue_edge(room, sid)
                if edge_token:
                    response["edge_token"] = edge_token
                if self.edge is not None:
                    response["edge"] = self.edge_info()
            return 200, response

    # --------------------------------------------------------------- /p2p/kick
    def _kick(self, name: str, payload: dict[str, Any], client: Any) -> tuple[int, dict]:
        """房主踢出一个在线成员：服务端强制“仅房主”（host_secret + host_sid）。

        从名册移除 target、挂入 blocked、清其在途配对令牌，并调 ``relay.evict`` 即时拆掉该
        成员当前桥接（两端都断，房主/加入者的 ``forward()`` 因 EOF 自然收尾）。注：端到端模型
        下无法区分同密码的新 sid 重连，若要禁止重加入需改密码（本轮不做）。
        """
        key = name.strip().lower()
        now = time.time()
        with self._lock:
            self._prune(now)
            room = self._rooms.get(key)
            if room is None:
                return 404, {"error": "房间未开放或已过期"}
            secret = str(payload.get("host_secret") or "")
            sid = _clamp_sid(payload.get("sid"))
            if not secret or not room.host_secret or not sid or sid != room.host_sid:
                return 403, {"error": "仅房主可操作"}
            if not hmac.compare_digest(secret, room.host_secret):
                return 403, {"error": "控制口令不符"}
            target = _clamp_sid(payload.get("target"))
            if not target or target not in room.peers:
                return 404, {"error": "目标不在房间内"}
            if target == room.host_sid:
                return 400, {"error": "不能踢出自己"}
            kicked = room.peers.pop(target, None)
            room.blocked.add(target)
            if kicked is not None and kicked.device:
                room.blocked_devices.add(kicked.device)
            room.last_seen = now
            peers = self._roster_payload(room)
        if self.relay is not None:
            self.relay.evict(key, target)
        if self.edge is not None:
            self.edge.evict(key, target)
        return 200, {"ok": True, "kicked": target, "peers": peers}

    # --------------------------------------------------------------- /p2p/rekey
    def _rekey(self, name: str, payload: dict[str, Any], client: Any) -> tuple[int, dict]:
        """房主重置房间密码：更新 auth_key 并即时清出所有非房主成员。

        密码只在房主本地派生，服务端只收到新的 auth_key。清空 blocked 列表，让房主可以
        用新密码选择性邀请之前踢出的设备（旧成员不知道新密码，无法继续占用房间）。
        """
        key = name.strip().lower()
        new_auth_key = _unhex(payload.get("auth_key"), _AUTH_KEY_BYTES)
        if new_auth_key is None:
            return 400, {"error": "缺少或非法的 auth_key"}
        now = time.time()
        removed: list[str] = []
        with self._lock:
            self._prune(now)
            room = self._rooms.get(key)
            if room is None:
                return 404, {"error": "房间未开放或已过期"}
            secret = str(payload.get("host_secret") or "")
            sid = _clamp_sid(payload.get("sid"))
            if not secret or not room.host_secret or not sid or sid != room.host_sid:
                return 403, {"error": "仅房主可操作"}
            if not hmac.compare_digest(secret, room.host_secret):
                return 403, {"error": "控制口令不符"}
            room.auth_key = new_auth_key
            for peer_sid in list(room.peers):
                if peer_sid != room.host_sid:
                    room.peers.pop(peer_sid, None)
                    removed.append(peer_sid)
            room.pending.clear()
            room.blocked.clear()
            room.blocked_devices.clear()
            # 新 auth_key 会改变加入者的派生 VIP：清空历史保留，仅留房主固定 .1
            room.vip_by_device = {room.owner: VIP_HOST} if room.owner else {}
            room.last_seen = now
            peers = self._roster_payload(room)
        if self.relay is not None:
            for peer_sid in removed:
                self.relay.evict(key, peer_sid)
        if self.edge is not None:
            for peer_sid in removed:
                self.edge.evict(key, peer_sid)
        return 200, {"ok": True, "peers": peers, "removed": len(removed)}


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
        #: (房间 code, sid) -> 参与该成员桥接的 StreamWriter 集合（一个桥两端都登记在双方 sid 下）
        self._sid_writers: dict[tuple[str, str], set] = {}
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
        await self._pair_and_bridge(reader, writer, existing, record)

    async def _pair_and_bridge(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
        first: dict[str, Any], record: "_Token"
    ) -> None:
        first_reader: asyncio.StreamReader = first["reader"]
        first_writer: asyncio.StreamWriter = first["writer"]
        sids = [record.requester, record.target]
        writers = [writer, first_writer]
        self._register(record.room, sids, writers)
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
            self._unregister(record.room, sids, writers)
            first["done"].set()

    # ------------------------------------------------------------ 桥接登记与驱逐
    def _register(self, room: str, sids: list[str], writers: list) -> None:
        with self._guard:
            for s in sids:
                if not s:
                    continue
                self._sid_writers.setdefault((room, s), set()).update(writers)

    def _unregister(self, room: str, sids: list[str], writers: list) -> None:
        with self._guard:
            for s in sids:
                if not s:
                    continue
                bucket = self._sid_writers.get((room, s))
                if bucket is not None:
                    bucket.difference_update(writers)
                    if not bucket:
                        self._sid_writers.pop((room, s), None)

    def evict(self, room: str, sid: str) -> int:
        """服务端侧即时拆桥：关掉登记在该 (room, sid) 的所有桥接套接字（两端），并清其在途令牌。

        在信令线程调用，故通过 ``call_soon_threadsafe`` 回到事件循环关 writer（asyncio 对象非线程安全）。
        返回关闭的套接字数。
        """
        with self._guard:
            writers = list(self._sid_writers.pop((room, sid), set()))
            dead = [t for t, rec in self._tokens.items()
                    if rec.room == room and sid in (rec.requester, rec.target)]
            for t in dead:
                self._tokens.pop(t, None)
        loop = self._loop
        if loop is None:
            return 0
        for w in writers:
            try:
                loop.call_soon_threadsafe(self._close, w)
            except RuntimeError:
                pass
        return len(writers)

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


# ---------------------------------------------------------------- UDP 边缘服务
@dataclass
class EdgeGrant:
    """边缘令牌台账条目：绑定房间 + 会话 sid + 设备 + 虚拟 IP。"""
    token: str
    room: str
    sid: str
    device: str
    vip: str
    expires: float


class EdgeTokenStore:
    """边缘令牌台账（线程安全）：签发、TTL 校验、一枚令牌只绑定一个 UDP 端点。

    UDP 边缘服务跑在自己的事件循环线程，信令跑在 HTTP 工作线程，因此这里用
    ``threading.Lock`` 而不是 asyncio 原语。
    """

    def __init__(self, ttl: float = 180.0) -> None:
        self.ttl = float(max(30, int(ttl or 180)))
        self._lock = threading.Lock()
        self._grants: dict[str, EdgeGrant] = {}
        #: token -> "ip:port"：绑定后不可换端点（换端点需信令下发的新令牌）
        self._bindings: dict[str, str] = {}

    def _prune(self, now: float) -> None:
        for token in [t for t, g in self._grants.items() if g.expires < now]:
            self._grants.pop(token, None)
            self._bindings.pop(token, None)

    def issue(self, *, room: str, sid: str, device: str, vip: str) -> str:
        token = secrets.token_urlsafe(24)
        now = time.time()
        with self._lock:
            self._prune(now)
            self._grants[token] = EdgeGrant(
                token=token, room=room, sid=sid, device=device, vip=vip,
                expires=now + self.ttl,
            )
        return token

    def valid(self, token: str) -> bool:
        now = time.time()
        with self._lock:
            self._prune(now)
            return token in self._grants

    def claim(self, token: str, endpoint: str) -> EdgeGrant | None:
        """校验并绑定端点；令牌不存在/过期或已绑定到别的端点时返回 None。"""
        now = time.time()
        with self._lock:
            self._prune(now)
            grant = self._grants.get(token)
            if grant is None:
                return None
            bound = self._bindings.get(token)
            if bound is not None and bound != endpoint:
                return None
            self._bindings[token] = endpoint
            return grant


@dataclass
class _Endpoint:
    room: str
    sid: str
    vip: str
    addr: tuple[str, int]
    last_seen: float


class _EdgeProtocol(asyncio.DatagramProtocol):
    def __init__(self, owner: "UdpEdgeServer") -> None:
        self._owner = owner

    def datagram_received(self, data: bytes, addr: Any) -> None:
        self._owner._on_datagram(data, addr)


class UdpEdgeServer:
    """UDP 边缘服务：登记公网端点 + 按目的 sid 转发密文。

    协议（单包，明文部分只有控制头）：

    - ``HELLO <token>`` → ``OK <sid> <vip>``：登记该会话的观测 ``ip:port``；
      同一令牌只允许绑定一个端点。
    - ``R <dst_sid>`` + 载荷：把载荷原样转发给同房间目标端点（服务端只见密文）。
    - 其他报文一律丢弃，绝不开放任意转发。

    只服务已登记端点；单房间累计转发字节超过 ``p2p_udp_bytes_cap``、或端点空闲超过
    ``p2p_room_ttl`` 即停止转发，避免沦为开放 UDP 中继。
    """

    _IDLE_DEFAULT = 180.0
    _SWEEP_INTERVAL = 30.0

    def __init__(
        self,
        settings: Any,
        *,
        host: str = "0.0.0.0",
        port: int | None = None,
        bytes_cap: int | None = None,
        token_ttl: float | None = None,
        idle: float | None = None,
    ) -> None:
        self.host = host
        self.port = int(port if port is not None else getattr(settings, "p2p_udp_port", 0) or 0)
        self.bytes_cap = int(
            bytes_cap if bytes_cap is not None
            else getattr(settings, "p2p_udp_bytes_cap", 0) or 0
        )
        self.idle = float(
            idle if idle is not None
            else getattr(settings, "p2p_room_ttl", self._IDLE_DEFAULT) or self._IDLE_DEFAULT
        )
        ttl = token_ttl if token_ttl is not None else max(
            60, getattr(settings, "p2p_edge_token_ttl", 180)
        )
        self.tokens = EdgeTokenStore(ttl)
        self._lock = threading.Lock()
        self._endpoints: dict[tuple[str, str], _Endpoint] = {}
        self._addr_map: dict[tuple[str, int], tuple[str, str]] = {}
        self._room_bytes: dict[str, int] = {}
        self._last_sweep = 0.0
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._transport: Any = None
        self._stopped: asyncio.Event | None = None
        self._stop = threading.Event()
        self.forwarded = 0
        self.dropped = 0

    # -------------------------------------------------------------- 令牌/端点查询
    def issue_token(self, *, room: str, sid: str, device: str, vip: str) -> str:
        return self.tokens.issue(room=room, sid=sid, device=device, vip=vip)

    def token_valid(self, token: str) -> bool:
        return self.tokens.valid(token) if token else False

    def endpoint(self, room: str, sid: str) -> str:
        """返回该会话登记的公网端点 "ip:port"；未登记或空闲超时返回空串。"""
        now = time.time()
        with self._lock:
            self._sweep(now)
            record = self._endpoints.get((room, sid))
            if record is None:
                return ""
            return f"{record.addr[0]}:{record.addr[1]}"

    def evict(self, room: str, sid: str) -> None:
        with self._lock:
            record = self._endpoints.pop((room, sid), None)
            if record is not None and self._addr_map.get(record.addr) == (room, sid):
                self._addr_map.pop(record.addr, None)

    def evict_room(self, room: str) -> None:
        with self._lock:
            for key in [k for k in self._endpoints if k[0] == room]:
                record = self._endpoints.pop(key, None)
                if record is not None and self._addr_map.get(record.addr) == key:
                    self._addr_map.pop(record.addr, None)
            self._room_bytes.pop(room, None)

    # ---------------------------------------------------------------- 线程生命周期
    def start(self) -> None:
        if self._thread is not None or self.port <= 0:
            return
        self._thread = threading.Thread(target=self._run, name="p2p-udp-edge", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 6.0) -> None:
        self._stop.set()
        loop, stopped = self._loop, self._stopped
        if loop is not None and stopped is not None:
            try:
                loop.call_soon_threadsafe(stopped.set)
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
                pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
            except RuntimeError:
                pass
            loop.close()
            self._loop = None

    async def _serve(self) -> None:
        loop = asyncio.get_running_loop()
        self._stopped = asyncio.Event()
        try:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _EdgeProtocol(self), local_addr=(self.host, self.port),
            )
        except OSError:
            self._stopped = None
            return
        self._transport = transport
        try:
            if self._stop.is_set():
                return
            await self._stopped.wait()
        finally:
            transport.close()
            self._transport = None

    # ------------------------------------------------------------------ 报文处理
    def _send(self, data: bytes, addr: Any) -> None:
        transport = self._transport
        if transport is None:
            return
        try:
            transport.sendto(data, addr)
        except (OSError, ConnectionError):
            pass

    def _sweep(self, now: float) -> None:
        """回收空闲端点与房间字节计数（调用方持锁，最多每 30s 扫一次）。"""
        if now - self._last_sweep < self._SWEEP_INTERVAL:
            return
        self._last_sweep = now
        for key, record in list(self._endpoints.items()):
            if now - record.last_seen > self.idle:
                self._endpoints.pop(key, None)
                if self._addr_map.get(record.addr) == key:
                    self._addr_map.pop(record.addr, None)
        live_rooms = {key[0] for key in self._endpoints}
        for room in list(self._room_bytes):
            if room not in live_rooms:
                self._room_bytes.pop(room, None)

    def _on_datagram(self, data: bytes, addr: Any) -> None:
        now = time.time()
        with self._lock:
            self._sweep(now)
        if data.startswith(b"HELLO "):
            self._hello(data[6:].strip(), addr, now)
            return
        if data.startswith(b"R "):
            self._relay(data, addr, now)
            return
        self.dropped += 1   # 未知报文：不转发

    def _hello(self, token: bytes, addr: Any, now: float) -> None:
        try:
            text = token.decode("ascii")
        except UnicodeDecodeError:
            self.dropped += 1
            return
        endpoint = f"{addr[0]}:{addr[1]}"
        grant = self.tokens.claim(text, endpoint)
        if grant is None:
            self.dropped += 1
            self._send(b"ERR bad-token\n", addr)
            return
        key = (grant.room, grant.sid)
        with self._lock:
            old = self._addr_map.get(addr)
            if old is not None and old != key:
                self._endpoints.pop(old, None)
            self._endpoints[key] = _Endpoint(
                room=grant.room, sid=grant.sid, vip=grant.vip, addr=addr, last_seen=now,
            )
            self._addr_map[addr] = key
        self._send(f"OK {grant.sid} {grant.vip}\n".encode("ascii"), addr)

    def _relay(self, data: bytes, addr: Any, now: float) -> None:
        body = data[2:]
        if len(body) < 32:
            self.dropped += 1
            return
        try:
            dst_sid = body[:32].decode("ascii")
        except UnicodeDecodeError:
            self.dropped += 1
            return
        if not _clamp_sid(dst_sid):
            self.dropped += 1
            return
        payload = body[32:]
        target_addr: Any = None
        with self._lock:
            source_key = self._addr_map.get(addr)
            if source_key is None:
                self.dropped += 1
                return
            room, sid = source_key
            source = self._endpoints.get(source_key)
            target = self._endpoints.get((room, dst_sid))
            if source is None or target is None or dst_sid == sid:
                self.dropped += 1
                return
            used = self._room_bytes.get(room, 0) + len(payload)
            if self.bytes_cap and used > self.bytes_cap:
                self.dropped += 1
                return
            self._room_bytes[room] = used
            source.last_seen = now
            target_addr = target.addr
        self.forwarded += 1
        self._send(payload, target_addr)
