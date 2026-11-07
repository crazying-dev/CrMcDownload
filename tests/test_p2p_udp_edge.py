"""UDP 边缘服务：HELLO 端点登记、令牌绑定、按 sid 中继与配额。

这些用例直接调用 ``_on_datagram`` 并用假 transport 截获回包，不依赖真实套接字，
保证 CI 上稳定。
"""

from __future__ import annotations

import time

import pytest

from server_mirror.p2p import EdgeTokenStore, UdpEdgeServer

ROOM = "room1"
SID_A = "a" * 32
SID_B = "b" * 32
ADDR_A = ("1.1.1.1", 1000)
ADDR_B = ("2.2.2.2", 2000)


class _Settings:
    p2p_udp_port = 25578
    p2p_udp_bytes_cap = 512 * 1024 * 1024
    p2p_room_ttl = 180
    p2p_edge_token_ttl = 180


class _FakeTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple]] = []

    def sendto(self, data: bytes, addr: tuple) -> None:
        self.sent.append((bytes(data), tuple(addr)))

    def close(self) -> None:
        pass


@pytest.fixture()
def edge():
    server = UdpEdgeServer(_Settings(), port=0)
    transport = _FakeTransport()
    server._transport = transport  # 跳过真实套接字，直接喂报文
    yield server
    server._transport = None


def _hello(edge, token: str, addr: tuple) -> None:
    edge._on_datagram(b"HELLO " + token.encode("ascii"), addr)


def test_hello_registers_endpoint_and_replies(edge):
    token = edge.issue_token(room=ROOM, sid=SID_A, device="d" * 32, vip="10.66.0.2")
    _hello(edge, token, ADDR_A)
    assert edge.endpoint(ROOM, SID_A) == "1.1.1.1:1000"
    data, addr = edge._transport.sent[-1]
    assert data == f"OK {SID_A} 10.66.0.2\n".encode("ascii")
    assert addr == ADDR_A


def test_token_binds_only_one_endpoint(edge):
    token = edge.issue_token(room=ROOM, sid=SID_A, device="d" * 32, vip="10.66.0.2")
    _hello(edge, token, ADDR_A)
    # 同一令牌从另一个端点 HELLO：拒绝，原登记不变
    _hello(edge, token, ADDR_B)
    assert edge.endpoint(ROOM, SID_A) == "1.1.1.1:1000"
    assert edge._transport.sent[-1][0].startswith(b"ERR ")
    # 同一端点重复 HELLO 幂等
    _hello(edge, token, ADDR_A)
    assert edge._transport.sent[-1][0].startswith(b"OK ")


def test_unknown_and_expired_token_rejected(edge):
    _hello(edge, "not-a-token", ADDR_A)
    assert edge._transport.sent[-1][0].startswith(b"ERR ")
    token = edge.issue_token(room=ROOM, sid=SID_A, device="d" * 32, vip="10.66.0.2")
    edge.tokens._grants[token].expires = time.time() - 1
    _hello(edge, token, ADDR_A)
    assert edge._transport.sent[-1][0].startswith(b"ERR ")
    assert edge.endpoint(ROOM, SID_A) == ""


def test_relay_forwards_ciphertext_to_target(edge):
    _hello(edge, edge.issue_token(room=ROOM, sid=SID_A, device="", vip="10.66.0.2"), ADDR_A)
    _hello(edge, edge.issue_token(room=ROOM, sid=SID_B, device="", vip="10.66.0.3"), ADDR_B)
    edge._on_datagram(b"R " + SID_B.encode("ascii") + b"cipher-blob", ADDR_A)
    data, addr = edge._transport.sent[-1]
    assert data == b"cipher-blob"
    assert addr == ADDR_B
    assert edge.forwarded == 1


def test_relay_rejects_unregistered_sender_and_target(edge):
    _hello(edge, edge.issue_token(room=ROOM, sid=SID_A, device="", vip="10.66.0.2"), ADDR_A)
    before = len(edge._transport.sent)
    # 未登记的发送方
    edge._on_datagram(b"R " + SID_A.encode("ascii") + b"x", ("9.9.9.9", 9))
    # 登记了但目标是未知 sid
    edge._on_datagram(b"R " + ("c" * 32).encode("ascii") + b"x", ADDR_A)
    assert len(edge._transport.sent) == before
    assert edge.dropped == 2


def test_relay_does_not_cross_rooms(edge):
    _hello(edge, edge.issue_token(room=ROOM, sid=SID_A, device="", vip="10.66.0.2"), ADDR_A)
    _hello(edge, edge.issue_token(room="room2", sid=SID_B, device="", vip="10.66.0.3"), ADDR_B)
    edge._on_datagram(b"R " + SID_B.encode("ascii") + b"x", ADDR_A)
    assert edge.forwarded == 0
    assert edge.dropped == 1


def test_relay_bytes_cap_stops_forwarding(edge):
    edge.bytes_cap = 10
    _hello(edge, edge.issue_token(room=ROOM, sid=SID_A, device="", vip="10.66.0.2"), ADDR_A)
    _hello(edge, edge.issue_token(room=ROOM, sid=SID_B, device="", vip="10.66.0.3"), ADDR_B)
    edge._on_datagram(b"R " + SID_B.encode("ascii") + b"123456", ADDR_A)  # 6 <= 10
    assert edge.forwarded == 1
    edge._on_datagram(b"R " + SID_B.encode("ascii") + b"123456", ADDR_A)  # 12 > 10
    assert edge.forwarded == 1


def test_evict_removes_endpoint(edge):
    _hello(edge, edge.issue_token(room=ROOM, sid=SID_A, device="", vip="10.66.0.2"), ADDR_A)
    assert edge.endpoint(ROOM, SID_A)
    edge.evict(ROOM, SID_A)
    assert edge.endpoint(ROOM, SID_A) == ""
    edge.evict_room(ROOM)
    assert edge._endpoints == {}


def test_edge_token_store_ttl_and_reissue():
    store = EdgeTokenStore(ttl=60)
    token = store.issue(room=ROOM, sid=SID_A, device="d" * 32, vip="10.66.0.2")
    assert store.valid(token)
    grant = store.claim(token, "1.1.1.1:1000")
    assert grant is not None and grant.room == ROOM and grant.sid == SID_A
    assert grant.device == "d" * 32 and grant.vip == "10.66.0.2"
    store._grants[token].expires = time.time() - 1
    assert not store.valid(token)
    assert store.claim(token, "1.1.1.1:1000") is None
