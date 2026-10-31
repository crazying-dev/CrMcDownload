"""离线联机信令客户端：走镜像 ``/p2p/*`` 创建/开放/加入房间，维护名册与中继配对。

所有信令请求都发向 ``download_base``（复用 :class:`Downloader.request_json` 的
令牌-origin 规则），但房间凭证（auth_key / proof）与全局下载令牌相互独立——准入
靠 proof，不依赖 ``X-OffCraft-Token``。房间用「全局唯一房间名 + 密码」标识；密码
只在本地派生密钥，绝不入库或上传。

生命周期（与服务端对齐）：
- :meth:`create`  专用入口登记一个新房间名（每设备配额，名字全局唯一）；
- :meth:`open`   房主开一个实时会话供加入（房间须已由本设备创建）；
- :meth:`join`   加入者凭密码派生的 proof 进入已开放会话（不存在/未开放即报错，绝不建房）；
- :meth:`list_rooms` / :meth:`delete` 查询/删除本设备创建过的房间。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .crypto import (
    canonical_name,
    derive_room_keys,
    make_proof,
    new_identity,
    RoomKeys,
)

__all__ = ["PeerInfo", "RoomClient"]


@dataclass(slots=True)
class PeerInfo:
    pid: str
    addr: str
    world: str = ""
    port: int = 0

    @classmethod
    def from_roster(cls, item: dict[str, Any]) -> "PeerInfo":
        return cls(
            pid=str(item.get("pid") or ""),
            addr=str(item.get("addr") or ""),
            world=str(item.get("world") or ""),
            port=int(item.get("port") or 0),
        )


@dataclass(slots=True)
class RoomClient:
    downloader: Any
    mirror: Any
    name: str
    password: str
    device_id: str = ""
    world: str = ""
    port: int = 0
    keys: RoomKeys = field(init=False)
    pub: str = field(init=False, default="")
    nonce: str = field(init=False, default="")
    sid: str = field(init=False, default="")
    host_sid: str = field(init=False, default="")
    peers: list[PeerInfo] = field(init=False, default_factory=list)
    relay: dict[str, Any] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        self.name = canonical_name(self.name)
        if not self.name:
            raise ValueError("房间名不能为空")
        if not self.password:
            raise ValueError("房间密码不能为空")
        self.keys = derive_room_keys(self.password, self.name)
        self.pub, self.nonce = new_identity()

    # -------------------------------------------------------------- 信令请求
    def _post(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = self.mirror.api_url(f"/p2p/{action}")
        data = self.downloader.request_json(url, "POST", body=payload)
        return data if isinstance(data, dict) else {}

    def _apply_join(self, resp: dict[str, Any]) -> dict[str, Any]:
        self.sid = str(resp.get("sid") or "")
        self.host_sid = str(resp.get("host") or "")
        self.relay = dict(resp.get("relay") or {})
        self.peers = [PeerInfo.from_roster(p) for p in (resp.get("peers") or [])]
        return resp

    # -------------------------------------------------------------- 房间管理
    def create(self) -> dict[str, Any]:
        """专用入口：登记一个新房间（全局唯一名 + 每设备配额）。"""
        return self._post("create", {"room": self.name, "device_id": self.device_id})

    def list_rooms(self) -> dict[str, Any]:
        """查询本设备创建过的房间与剩余额度。"""
        return self._post("list", {"device_id": self.device_id})

    def delete(self) -> dict[str, Any]:
        """删除本设备创建的一个房间（释放名额、名字可复用）。"""
        return self._post("delete", {"room": self.name, "device_id": self.device_id})

    # -------------------------------------------------------------- 会话
    def open(self) -> dict[str, Any]:
        """房主开一个实时会话：登记从密码派生的 auth_key（服务端仅用于校验他人 proof）。"""
        resp = self._post("open", {
            "room": self.name,
            "client_pub": self.pub,
            "auth_key": self.keys.auth_key.hex(),
            "world": self.world,
            "port": self.port,
            "device_id": self.device_id,
        })
        return self._apply_join(resp)

    def join(self) -> dict[str, Any]:
        """加入者凭 proof 进入已开放会话；房间不存在/未开放由服务端报错，绝不自动建房。"""
        resp = self._post("join", {
            "room": self.name,
            "client_pub": self.pub,
            "nonce": self.nonce,
            "proof": make_proof(self.keys.auth_key, self.pub, self.nonce),
            "device_id": self.device_id,
        })
        return self._apply_join(resp)

    def announce(self, world: str | None = None, port: int | None = None) -> dict[str, Any]:
        payload = {
            "room": self.name,
            "sid": self.sid,
            "world": self.world if world is None else world,
            "port": self.port if port is None else port,
        }
        return self._post("announce", payload)

    def request_relay(self, target_sid: str) -> dict[str, Any]:
        payload = {"room": self.name, "sid": self.sid, "peer": target_sid}
        resp = self._post("relay", payload)
        if resp.get("relay"):
            self.relay = dict(resp["relay"])
        return resp

    def leave(self) -> None:
        if not self.sid:
            return
        try:
            self._post("leave", {"room": self.name, "sid": self.sid})
        except Exception:  # noqa: BLE001 - 离开尽力而为，不影响本地清理
            pass
