"""RelayServer 桥接登记（_register/_unregister）与即时拆桥（evict）的簿记逻辑。

不启动事件循环（port=0、loop=None）：evict 仍应清理 (room,sid) 登记并撤销相关在途令牌；
真正的 socket 关闭需活循环，故这里只验证簿记与令牌回收。
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from server_mirror.p2p import RelayServer


@dataclass
class _Settings:
    p2p_relay_port: int = 25577
    p2p_relay_bytes_cap: int = 512 * 1024 * 1024
    p2p_room_ttl: int = 180


class _FakeWriter:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_register_then_evict_purges_map_and_pending_token():
    relay = RelayServer(_Settings(), port=0)
    host_sid, peer_sid = secrets.token_hex(16), secrets.token_hex(16)
    w1, w2 = _FakeWriter(), _FakeWriter()
    relay._register("room1", [peer_sid, host_sid], [w1, w2])
    token = relay.new_token("room1", requester=peer_sid, target=host_sid)
    assert (relay._sid_writers.get(("room1", peer_sid)) == {w1, w2})

    relay.evict("room1", peer_sid)
    # 登记被清；在途令牌（requester/target 命中 peer_sid）被撤销
    assert ("room1", peer_sid) not in relay._sid_writers
    assert token not in relay._tokens


def test_unregister_removes_only_given_writers():
    relay = RelayServer(_Settings(), port=0)
    sid = secrets.token_hex(16)
    w1, w2 = _FakeWriter(), _FakeWriter()
    relay._register("room1", [sid], [w1, w2])
    relay._unregister("room1", [sid], [w1])
    assert relay._sid_writers.get(("room1", sid)) == {w2}
    relay._unregister("room1", [sid], [w2])
    assert ("room1", sid) not in relay._sid_writers


def test_evict_unknown_room_is_noop():
    relay = RelayServer(_Settings(), port=0)
    assert relay.evict("ghost", secrets.token_hex(16)) == 0
