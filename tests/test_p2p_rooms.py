"""房间持久登记：全局唯一名（大小写不敏感）、每设备现存配额、删除释放名额、列表与属主。

分两层：DB 层直接验证 ``Database`` 的 create/delete/list 语义；信令层验证
``SignalingRouter`` 的 create/list/delete 端点响应码。
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

import pytest

from server_mirror.db import Database
from server_mirror.p2p import SignalingRouter

CLIENT = ("1.2.3.4", 5678)


@dataclass
class _Settings:
    p2p_relay_port: int = 25577
    p2p_relay_host: str = ""
    p2p_max_rooms: int = 256
    p2p_max_rooms_per_device: int = 10
    p2p_room_ttl: int = 180
    host: str = "127.0.0.1"


# ---------------------------------------------------------------- DB 层
def test_db_create_unique_and_case_insensitive(tmp_path):
    db = Database(tmp_path / "d.db")
    try:
        assert db.create_room("RoomA", "RoomA", "dev1", max_per_device=10) == "ok"
        assert db.create_room("rooma", "rooma", "dev2", max_per_device=10) == "exists"
        # 不同名同设备可多个
        assert db.create_room("RoomB", "RoomB", "dev1", max_per_device=10) == "ok"
    finally:
        db.close()


def test_db_quota_counts_existing_and_delete_frees(tmp_path):
    db = Database(tmp_path / "d.db")
    try:
        for i in range(10):
            assert db.create_room(f"r{i}", f"r{i}", "dev1", max_per_device=10) == "ok"
        assert db.create_room("r10", "r10", "dev1", max_per_device=10) == "quota"
        # 别的设备不受影响
        assert db.create_room("r10", "r10", "dev2", max_per_device=10) == "ok"
        # 删除一个后本设备腾出名额
        assert db.delete_room("r3", "dev1") is True
        assert db.create_room("r11", "r11", "dev1", max_per_device=10) == "ok"
    finally:
        db.close()


def test_db_delete_only_owner(tmp_path):
    db = Database(tmp_path / "d.db")
    try:
        db.create_room("only", "only", "dev1", max_per_device=10)
        assert db.delete_room("only", "dev2") is False
        assert db.get_room("only") is not None
        assert db.delete_room("only", "dev1") is True
        assert db.get_room("only") is None
    finally:
        db.close()


def test_db_list_and_count(tmp_path):
    db = Database(tmp_path / "d.db")
    try:
        db.create_room("a", "a", "dev1", max_per_device=10)
        db.create_room("b", "b", "dev1", max_per_device=10)
        db.create_room("c", "c", "dev2", max_per_device=10)
        rooms = db.list_rooms("dev1")
        assert {r["name"] for r in rooms} == {"a", "b"}
        assert db.count_rooms("dev1") == 2
    finally:
        db.close()


# ------------------------------------------------------------ 信令层
@pytest.fixture()
def router(tmp_path):
    from server_mirror.p2p import RelayServer

    db = Database(tmp_path / "r.db")
    relay = RelayServer(_Settings(), port=0)
    yield SignalingRouter(settings=_Settings(), relay=relay, db=db)
    db.close()


def test_signaling_create_duplicate_409(router):
    dev = secrets.token_hex(16)
    status, _ = router.handle("/p2p/create", {"room": "MyRoom", "device_id": dev}, CLIENT)
    assert status == 200
    status, _ = router.handle("/p2p/create", {"room": "myroom", "device_id": dev}, CLIENT)
    assert status == 409


def test_signaling_create_bad_device_400(router):
    status, _ = router.handle("/p2p/create", {"room": "R", "device_id": "zz"}, CLIENT)
    assert status == 400


def test_signaling_create_quota_403(router):
    dev = secrets.token_hex(16)
    for i in range(10):
        status, _ = router.handle("/p2p/create", {"room": f"room{i}", "device_id": dev}, CLIENT)
        assert status == 200
    status, data = router.handle("/p2p/create", {"room": "room10", "device_id": dev}, CLIENT)
    assert status == 403
    assert "10" in data["error"]


def test_signaling_delete_frees_slot(router):
    dev = secrets.token_hex(16)
    for i in range(10):
        router.handle("/p2p/create", {"room": f"room{i}", "device_id": dev}, CLIENT)
    status, _ = router.handle("/p2p/delete", {"room": "room0", "device_id": dev}, CLIENT)
    assert status == 200
    status, _ = router.handle("/p2p/create", {"room": "newroom", "device_id": dev}, CLIENT)
    assert status == 200


def test_signaling_delete_only_owner(router):
    dev = secrets.token_hex(16)
    other = secrets.token_hex(16)
    router.handle("/p2p/create", {"room": "mine", "device_id": dev}, CLIENT)
    status, _ = router.handle("/p2p/delete", {"room": "mine", "device_id": other}, CLIENT)
    assert status == 403
    status, _ = router.handle("/p2p/delete", {"room": "ghost", "device_id": dev}, CLIENT)
    assert status == 404


def test_signaling_list_returns_rooms_and_remaining(router):
    dev = secrets.token_hex(16)
    router.handle("/p2p/create", {"room": "aa", "device_id": dev}, CLIENT)
    router.handle("/p2p/create", {"room": "bb", "device_id": dev}, CLIENT)
    status, data = router.handle("/p2p/list", {"device_id": dev}, CLIENT)
    assert status == 200
    assert {r["name"] for r in data["rooms"]} == {"aa", "bb"}
    assert data["remaining"] == 8 and data["limit"] == 10
