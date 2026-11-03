"""``/p2p`` 信令：create/open/join/announce/relay/leave、proof 准入、房间规模与观测地址。

房间语义（重构后）：房主先 ``create`` 登记全局唯一房间名（专用入口），再 ``open`` 开一个
实时会话并登记从密码派生的 auth_key；加入者 ``join`` 只连已开放会话、绝不自动建房。
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from hashlib import sha256

import pytest

from server_mirror.p2p import SignalingRouter, proof_message

CLIENT = ("1.2.3.4", 5678)
DEVICE = secrets.token_hex(16)   # 稳定的合法 device_id（32 位 hex）


@dataclass
class _Settings:
    p2p_relay_port: int = 25577
    p2p_relay_host: str = ""
    p2p_max_rooms: int = 256
    p2p_max_rooms_per_device: int = 10
    p2p_room_ttl: int = 180
    p2p_relay_bytes_cap: int = 512 * 1024 * 1024
    host: str = "127.0.0.1"


def _open_host(router, code: str, auth_key: bytes, *, pub: str, world="W", port=25565,
               device=DEVICE):
    """先 create 登记房间名，再 open 开实时会话；返回 open 的结果。"""
    router.handle("/p2p/create", {"room": code, "device_id": device}, CLIENT)
    return router.handle("/p2p/open", {
        "room": code, "client_pub": pub, "auth_key": auth_key.hex(),
        "world": world, "port": port, "device_id": device,
    }, CLIENT)


def _joiner_proof(auth_key: bytes, pub: str, nonce: str) -> str:
    return hmac.new(auth_key, proof_message(bytes.fromhex(pub), bytes.fromhex(nonce)), sha256).hexdigest()


@pytest.fixture()
def router(tmp_path):
    from server_mirror.db import Database
    from server_mirror.p2p import RelayServer

    settings = _Settings()
    relay = RelayServer(settings, port=0)  # 不启动循环，仅用于令牌登记
    db = Database(tmp_path / "test.db")
    yield SignalingRouter(settings=settings, relay=relay, db=db)
    db.close()


def test_host_open_returns_sid_and_observed_addr(router):
    status, data = _open_host(router, "ROOM1", secrets.token_bytes(32), pub=secrets.token_hex(32))
    assert status == 200
    assert data["sid"] == data["host"]
    assert len(data["peers"]) == 1
    assert data["peers"][0]["addr"] == "1.2.3.4:5678"   # 观测地址来自 client_address
    assert data["relay"] == {"host": "127.0.0.1", "port": 25577}


def test_joiner_valid_proof_admitted(router):
    auth_key = secrets.token_bytes(32)
    _open_host(router, "ROOM1", auth_key, pub=secrets.token_hex(32))
    pub = secrets.token_hex(32)
    nonce = secrets.token_hex(16)
    status, data = router.handle("/p2p/join", {
        "room": "ROOM1", "client_pub": pub, "nonce": nonce,
        "proof": _joiner_proof(auth_key, pub, nonce),
    }, CLIENT)
    assert status == 200
    assert data["sid"] and data["host"]
    assert len(data["peers"]) == 2


def test_joiner_invalid_proof_rejected(router):
    auth_key = secrets.token_bytes(32)
    _open_host(router, "ROOM1", auth_key, pub=secrets.token_hex(32))
    pub = secrets.token_hex(32)
    nonce = secrets.token_hex(16)
    status, _ = router.handle("/p2p/join", {
        "room": "ROOM1", "client_pub": pub, "nonce": nonce, "proof": "00" * 32,
    }, CLIENT)
    assert status == 403


def test_join_unknown_room_rejected(router):
    # 房间从未 create/open：join 一律 404，绝不自动建房
    status, _ = router.handle("/p2p/join", {
        "room": "GHOST", "client_pub": secrets.token_hex(32),
        "nonce": secrets.token_hex(16), "proof": "00" * 32,
    }, CLIENT)
    assert status == 404


def test_join_never_creates_even_if_registered(router):
    # 已 create 登记但未 open：join 仍 404（不自动建房/开房）
    router.handle("/p2p/create", {"room": "ROOM9", "device_id": DEVICE}, CLIENT)
    status, _ = router.handle("/p2p/join", {
        "room": "ROOM9", "client_pub": secrets.token_hex(32),
        "nonce": secrets.token_hex(16), "proof": "00" * 32,
    }, CLIENT)
    assert status == 404


def test_open_requires_registered_room(router):
    status, _ = router.handle("/p2p/open", {
        "room": "NOPE", "client_pub": secrets.token_hex(32),
        "auth_key": secrets.token_bytes(32).hex(), "device_id": DEVICE,
    }, CLIENT)
    assert status == 404


def test_open_requires_matching_owner(router):
    router.handle("/p2p/create", {"room": "ROOM1", "device_id": DEVICE}, CLIENT)
    other = secrets.token_hex(16)
    status, _ = router.handle("/p2p/open", {
        "room": "ROOM1", "client_pub": secrets.token_hex(32),
        "auth_key": secrets.token_bytes(32).hex(), "device_id": other,
    }, CLIENT)
    assert status == 403


def test_open_requires_valid_auth_key(router):
    router.handle("/p2p/create", {"room": "ROOM1", "device_id": DEVICE}, CLIENT)
    status, _ = router.handle("/p2p/open", {
        "room": "ROOM1", "client_pub": secrets.token_hex(32),
        "auth_key": "not-hex", "device_id": DEVICE,
    }, CLIENT)
    assert status == 400


def test_reopen_same_room_conflict(router):
    auth_key = secrets.token_bytes(32)
    _open_host(router, "ROOM1", auth_key, pub=secrets.token_hex(32))
    status, _ = _open_host(router, "ROOM1", auth_key, pub=secrets.token_hex(32))
    assert status == 409


def test_room_scale_cap(router):
    router.settings.p2p_max_rooms = 1
    status, _ = _open_host(router, "ROOM1", secrets.token_bytes(32), pub=secrets.token_hex(32))
    assert status == 200
    status, _ = _open_host(router, "ROOM2", secrets.token_bytes(32), pub=secrets.token_hex(32))
    assert status == 503


def test_relay_allocates_token_and_announce_delivers(router):
    auth_key = secrets.token_bytes(32)
    _, host = _open_host(router, "ROOM1", auth_key, pub=secrets.token_hex(32))
    host_sid = host["host"]
    pub = secrets.token_hex(32)
    nonce = secrets.token_hex(16)
    _, join = router.handle("/p2p/join", {
        "room": "ROOM1", "client_pub": pub, "nonce": nonce,
        "proof": _joiner_proof(auth_key, pub, nonce),
    }, CLIENT)
    joiner_sid = join["sid"]
    status, relay = router.handle("/p2p/relay", {
        "room": "ROOM1", "sid": joiner_sid, "peer": host_sid,
    }, CLIENT)
    assert status == 200
    token = relay["token"]
    # 房主 announce 领取待配对令牌
    status, ann = router.handle("/p2p/announce", {"room": "ROOM1", "sid": host_sid}, CLIENT)
    assert status == 200 and ann["relay_token"] == token
    # 令牌是一次性的：再次 announce 不再返回
    _, ann2 = router.handle("/p2p/announce", {"room": "ROOM1", "sid": host_sid}, CLIENT)
    assert ann2["relay_token"] == ""


def test_relay_requires_membership(router):
    auth_key = secrets.token_bytes(32)
    _, host = _open_host(router, "ROOM1", auth_key, pub=secrets.token_hex(32))
    status, _ = router.handle("/p2p/relay", {
        "room": "ROOM1", "sid": "deadbeef" * 4, "peer": host["host"],
    }, CLIENT)
    assert status == 403


def test_leave_removes_room_when_host_exits(router):
    auth_key = secrets.token_bytes(32)
    _, host = _open_host(router, "ROOM1", auth_key, pub=secrets.token_hex(32))
    host_sid = host["host"]
    status, _ = router.handle("/p2p/leave", {"room": "ROOM1", "sid": host_sid}, CLIENT)
    assert status == 200
    # 实时会话随之消失：加入者再进得到 404（房间登记仍在，但未开放）
    status, _ = router.handle("/p2p/join", {
        "room": "ROOM1", "client_pub": secrets.token_hex(32),
        "nonce": secrets.token_hex(16), "proof": "00" * 32,
    }, CLIENT)
    assert status == 404


def test_room_name_is_case_insensitive(router):
    auth_key = secrets.token_bytes(32)
    _open_host(router, "RoomOne", auth_key, pub=secrets.token_hex(32))
    pub = secrets.token_hex(32)
    nonce = secrets.token_hex(16)
    status, _ = router.handle("/p2p/join", {
        "room": "rOOMoNE", "client_pub": pub, "nonce": nonce,
        "proof": _joiner_proof(auth_key, pub, nonce),
    }, CLIENT)
    assert status == 200


def test_unknown_action_404(router):
    status, _ = router.handle("/p2p/nope", {"room": "R"}, CLIENT)
    assert status == 404


def _join_one(router, code, auth_key):
    pub = secrets.token_hex(32)
    nonce = secrets.token_hex(16)
    status, join = router.handle("/p2p/join", {
        "room": code, "client_pub": pub, "nonce": nonce,
        "proof": _joiner_proof(auth_key, pub, nonce),
    }, CLIENT)
    assert status == 200
    return join["sid"]


def test_open_returns_host_secret_not_in_roster(router):
    _, data = _open_host(router, "ROOM1", secrets.token_bytes(32), pub=secrets.token_hex(32))
    assert data.get("host_secret")
    assert all("host_secret" not in p for p in data["peers"])   # 不进名册


def test_roster_lists_members_and_requires_membership(router):
    auth_key = secrets.token_bytes(32)
    _, host = _open_host(router, "ROOM1", auth_key, pub=secrets.token_hex(32))
    host_sid = host["host"]
    _join_one(router, "ROOM1", auth_key)
    status, roster = router.handle("/p2p/roster", {"room": "ROOM1", "sid": host_sid}, CLIENT)
    assert status == 200 and len(roster["peers"]) == 2
    status, _ = router.handle("/p2p/roster", {"room": "ROOM1", "sid": "deadbeef" * 4}, CLIENT)
    assert status == 403


def test_kick_requires_host_secret_and_host_sid(router):
    auth_key = secrets.token_bytes(32)
    _, host = _open_host(router, "ROOM1", auth_key, pub=secrets.token_hex(32))
    host_sid, secret = host["host"], host["host_secret"]
    joiner_sid = _join_one(router, "ROOM1", auth_key)
    # 错误的 host_secret → 403
    status, _ = router.handle("/p2p/kick", {
        "room": "ROOM1", "sid": host_sid, "host_secret": "bad", "target": joiner_sid,
    }, CLIENT)
    assert status == 403
    # 口令对但调用者不是 host_sid（加入者伪造）→ 403
    status, _ = router.handle("/p2p/kick", {
        "room": "ROOM1", "sid": joiner_sid, "host_secret": secret, "target": host_sid,
    }, CLIENT)
    assert status == 403


def test_kick_removes_target_and_blocks_relay(router):
    auth_key = secrets.token_bytes(32)
    _, host = _open_host(router, "ROOM1", auth_key, pub=secrets.token_hex(32))
    host_sid, secret = host["host"], host["host_secret"]
    joiner_sid = _join_one(router, "ROOM1", auth_key)
    status, data = router.handle("/p2p/kick", {
        "room": "ROOM1", "sid": host_sid, "host_secret": secret, "target": joiner_sid,
    }, CLIENT)
    assert status == 200 and data["ok"] and data["kicked"] == joiner_sid
    assert all(p["pid"] != joiner_sid for p in data["peers"])
    # 被踢 sid 再用 relay → 403（已离名册 + blocked）
    status, _ = router.handle("/p2p/relay", {
        "room": "ROOM1", "sid": joiner_sid, "peer": host_sid,
    }, CLIENT)
    assert status == 403
    # 不能踢自己
    status, _ = router.handle("/p2p/kick", {
        "room": "ROOM1", "sid": host_sid, "host_secret": secret, "target": host_sid,
    }, CLIENT)
    assert status == 400


def test_kick_unknown_room_or_target(router):
    status, _ = router.handle("/p2p/kick", {
        "room": "GHOST", "sid": "a" * 32, "host_secret": "x", "target": "b" * 32,
    }, CLIENT)
    assert status == 404   # 房间未开放


def _join_with_device(router, code: str, auth_key: bytes, device: str) -> tuple[int, dict]:
    pub = secrets.token_hex(32)
    nonce = secrets.token_hex(16)
    return router.handle("/p2p/join", {
        "room": code, "client_pub": pub, "nonce": nonce,
        "proof": _joiner_proof(auth_key, pub, nonce), "device_id": device,
    }, CLIENT)


def test_kicked_device_cannot_rejoin_with_new_sid(router):
    """踢出后按稳定 device_id 封禁：换新 sid / 新公钥也不能回来。"""
    auth_key = secrets.token_bytes(32)
    _, host = _open_host(router, "ROOM1", auth_key, pub=secrets.token_hex(32))
    host_sid, secret = host["host"], host["host_secret"]
    joiner_device = secrets.token_hex(16)
    status, join = _join_with_device(router, "ROOM1", auth_key, joiner_device)
    assert status == 200
    status, _ = router.handle("/p2p/kick", {
        "room": "ROOM1", "sid": host_sid, "host_secret": secret, "target": join["sid"],
    }, CLIENT)
    assert status == 200
    # 同设备、新 sid、正确密码 → 仍被拒
    status, data = _join_with_device(router, "ROOM1", auth_key, joiner_device)
    assert status == 403
    assert "移出" in data["error"]
    # 另一台设备用正确密码 → 正常加入（封禁只针对设备，不封房间）
    status, _ = _join_with_device(router, "ROOM1", auth_key, secrets.token_hex(16))
    assert status == 200


def test_rekey_rotates_key_and_evicts_all_but_host(router):
    auth_key = secrets.token_bytes(32)
    _, host = _open_host(router, "ROOM1", auth_key, pub=secrets.token_hex(32))
    host_sid, secret = host["host"], host["host_secret"]
    device = secrets.token_hex(16)
    status, join = _join_with_device(router, "ROOM1", auth_key, device)
    assert status == 200
    new_key = secrets.token_bytes(32)
    status, data = router.handle("/p2p/rekey", {
        "room": "ROOM1", "sid": host_sid, "host_secret": secret, "auth_key": new_key.hex(),
    }, CLIENT)
    assert status == 200 and data["ok"] and data["removed"] == 1
    assert [p["pid"] for p in data["peers"]] == [host_sid]
    # 旧密码 proof 失效
    status, _ = _join_with_device(router, "ROOM1", auth_key, device)
    assert status == 403
    # 新密码 + 之前被移出的设备也可以回来（rekey 清空设备封禁）
    status, _ = _join_with_device(router, "ROOM1", new_key, device)
    assert status == 200
    # 旧 sid 已不在名册
    status, _ = router.handle("/p2p/relay", {
        "room": "ROOM1", "sid": join["sid"], "peer": host_sid,
    }, CLIENT)
    assert status == 403


def test_rekey_requires_host_and_valid_key(router):
    auth_key = secrets.token_bytes(32)
    _, host = _open_host(router, "ROOM1", auth_key, pub=secrets.token_hex(32))
    host_sid, secret = host["host"], host["host_secret"]
    joiner_sid = _join_one(router, "ROOM1", auth_key)
    status, _ = router.handle("/p2p/rekey", {
        "room": "ROOM1", "sid": joiner_sid, "host_secret": secret,
        "auth_key": secrets.token_bytes(32).hex(),
    }, CLIENT)
    assert status == 403
    status, _ = router.handle("/p2p/rekey", {
        "room": "ROOM1", "sid": host_sid, "host_secret": secret, "auth_key": "bad",
    }, CLIENT)
    assert status == 400
    status, _ = router.handle("/p2p/rekey", {
        "room": "GHOST", "sid": host_sid, "host_secret": secret,
        "auth_key": secrets.token_bytes(32).hex(),
    }, CLIENT)
    assert status == 404
