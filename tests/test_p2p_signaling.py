"""``/p2p`` 信令：join/announce/relay/leave、proof 准入、房间规模与观测地址。"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from hashlib import sha256

import pytest

from server_mirror.p2p import SignalingRouter, proof_message

CLIENT = ("1.2.3.4", 5678)


@dataclass
class _Settings:
    p2p_relay_port: int = 25577
    p2p_relay_host: str = ""
    p2p_max_rooms: int = 256
    p2p_room_ttl: int = 180
    p2p_relay_bytes_cap: int = 512 * 1024 * 1024
    host: str = "127.0.0.1"


def _open_host(router: SignalingRouter, code: str, auth_key: bytes, *, pub: str, world="W", port=25565):
    return router.handle("/p2p/join", {
        "room": code, "client_pub": pub, "nonce": secrets.token_hex(16),
        "is_host": True, "auth_key": auth_key.hex(), "world": world, "port": port,
    }, CLIENT)


def _joiner_proof(auth_key: bytes, pub: str, nonce: str) -> str:
    return hmac.new(auth_key, proof_message(bytes.fromhex(pub), bytes.fromhex(nonce)), sha256).hexdigest()


@pytest.fixture()
def router():
    from server_mirror.p2p import RelayServer

    settings = _Settings()
    relay = RelayServer(settings, port=0)  # 不启动循环，仅用于令牌登记
    return SignalingRouter(settings=settings, relay=relay)


def test_host_join_returns_sid_and_observed_addr(router):
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
    status, _ = router.handle("/p2p/join", {
        "room": "GHOST", "client_pub": secrets.token_hex(32),
        "nonce": secrets.token_hex(16), "proof": "00" * 32,
    }, CLIENT)
    assert status == 404


def test_host_join_requires_valid_auth_key(router):
    status, _ = router.handle("/p2p/join", {
        "room": "ROOM1", "client_pub": secrets.token_hex(32),
        "nonce": secrets.token_hex(16), "is_host": True, "auth_key": "not-hex",
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
    # 房间随之消失：加入者再进得到 404
    status, _ = router.handle("/p2p/join", {
        "room": "ROOM1", "client_pub": secrets.token_hex(32),
        "nonce": secrets.token_hex(16), "proof": "00" * 32,
    }, CLIENT)
    assert status == 404


def test_unknown_action_404(router):
    status, _ = router.handle("/p2p/nope", {"room": "R"}, CLIENT)
    assert status == 404
