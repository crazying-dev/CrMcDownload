"""探测层：SLP 编解码、SSRF 拒绝私网、后台一轮探测入库与热度排序。

禁网：``probe_once`` 对私网目标在建立连接前即被 SSRF 护栏拦下；``run_cycle``
用 monkeypatch 替换真实网络探测。
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

from server_mirror import servers
from server_mirror.db import Database
from server_mirror.servers import (
    ServerProbe,
    encode_varint,
    parse_slp_response,
    probe_once,
)


# --------------------------------------------------------------- varint
def test_encode_varint_boundaries():
    assert encode_varint(0) == b"\x00"
    assert encode_varint(127) == b"\x7f"
    assert encode_varint(128) == b"\x80\x01"
    assert encode_varint(255) == b"\xff\x01"
    assert encode_varint(-1) == b"\xff\xff\xff\xff\x0f"      # 补码 5 字节


def test_build_request_ends_with_status_request():
    payload = servers.build_request("mc.example", 25565)
    assert isinstance(payload, bytes) and len(payload) > 0
    # Handshake 之后只能是空的状态请求包，不能是 Login Start
    assert payload.endswith(b"\x01\x00")
    assert b"OffCraft-Mirror" not in payload


def test_decode_varint():
    assert servers._decode_varint(b"\x00") == (0, 1)
    assert servers._decode_varint(b"\x80\x01") == (128, 2)
    with pytest.raises(ValueError):
        servers._decode_varint(b"\x80")


def test_probe_once_parses_real_status_frame():
    async def scenario():
        async def handle(reader, writer):
            await reader.read(128)          # 请求内容不重要，这里只验证响应解析
            body = json.dumps({
                "version": {"name": "1.20.4", "protocol": 763},
                "players": {"online": 1, "max": 2},
                "description": "hi",
            }).encode()
            inner = b"\x00" + servers.encode_varint(len(body)) + body
            writer.write(servers.encode_varint(len(inner)) + inner)
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            return await probe_once("127.0.0.1", port, timeout=2.0, allow_private=True)

    result = asyncio.run(scenario())
    assert result["ok"] is True, result["error"]
    assert result["version"] == "1.20.4"
    assert result["online"] == 1 and result["max"] == 2


# ------------------------------------------------------------- 解析响应
def test_parse_slp_basic_fields():
    parsed = parse_slp_response({
        "version": {"name": "1.20.4", "protocol": 763},
        "players": {"online": 3, "max": 100,
                    "sample": [{"name": "Alice", "id": "uuid-1"}]},
        "description": "Hello World",
    })
    assert parsed["version"] == "1.20.4"
    assert parsed["online"] == 3 and parsed["max"] == 100
    assert parsed["players"] == [{"name": "Alice", "uuid": "uuid-1"}]
    assert parsed["motd"] == "Hello World"
    assert parsed["favicon"] is None


def test_parse_slp_nested_extra_motd_and_color_codes():
    parsed = parse_slp_response({
        "version": {"name": "1.19"},
        "description": {
            "text": "§a服务器",
            "extra": [{"text": "§r欢迎", "extra": [{"text": "玩家"}]}],
        },
    })
    assert parsed["motd"] == "服务器欢迎玩家"          # 递归拼接 + 去颜色码


def test_parse_slp_favicon_data_uri_decodes():
    raw = b"\x89PNG-fake"
    uri = "data:image/png;base64," + base64.b64encode(raw).decode()
    parsed = parse_slp_response({"version": {}, "favicon": uri})
    assert parsed["favicon"] == raw


def test_parse_slp_favicon_rejects_non_image():
    parsed = parse_slp_response({"version": {}, "favicon": "http://x/y.png"})
    assert parsed["favicon"] is None


def test_parse_slp_missing_sample_not_visible():
    parsed = parse_slp_response({
        "version": {"name": "1.20"},
        "players": {"online": 1, "max": 20},
        "enforceSecureProfile": True,
    })
    assert parsed["players"] == []
    assert parsed["online"] == 1


def test_parse_slp_tolerates_garbage():
    parsed = parse_slp_response({
        "version": None,
        "players": {"online": "abc", "max": None, "sample": [{"name": ""}, "junk"]},
        "description": 5,
    })
    assert parsed["online"] == 0 and parsed["max"] == 0
    assert parsed["players"] == []          # 无名/非 dict 条目被丢弃
    assert parsed["motd"] == "5"


# ------------------------------------------------------------- SSRF
def test_probe_once_rejects_loopback_without_connecting():
    result = asyncio.run(probe_once("127.0.0.1", 25565, timeout=2.0))
    assert result["ok"] is False
    assert result["error"]              # 带中文拒绝理由


def test_probe_once_rejects_private_range():
    result = asyncio.run(probe_once("10.0.0.5", 25565, timeout=2.0))
    assert result["ok"] is False
    assert result["error"]                      # 拒绝非公网地址，未发起连接


# --------------------------------------------------------- 后台探测入库
def test_run_cycle_records_results(tmp_path, monkeypatch):
    db = Database(tmp_path / "mirror.db")
    db.upsert_server("a.example", 25565)
    db.upsert_server("b.example", 25565)

    fake = {
        "a.example": {"host": "a.example", "port": 25565, "ok": True, "ts": 1.0,
                      "latency_ms": 30.0, "online": 8, "max": 50,
                      "version": "1.20.4", "motd": "A", "players": [{"name": "X", "uuid": "1"}],
                      "favicon": None, "error": ""},
        "b.example": {"host": "b.example", "port": 25565, "ok": False, "ts": 1.0,
                      "error": "无法连接"},
    }

    async def fake_probe(host, port, *, timeout=5.0, name="OffCraft-Mirror", allow_private=False):
        return fake[host]

    monkeypatch.setattr(servers, "probe_once", fake_probe)
    probe = ServerProbe(db, interval=15)
    count = asyncio.run(probe.run_cycle())
    assert count == 2

    entries, total = db.rank()
    assert total == 2
    assert entries[0]["host"] == "a.example"       # 在线 8 -> 更热
    assert db.detail("b.example", 25565)["ok"] in (0, False)
    db.close()


def test_seed_if_empty_imports_popular(tmp_path):
    import json
    popular = tmp_path / "popular_servers.json"
    popular.write_text(json.dumps({"servers": [
        {"host": "one.example", "port": 25565, "label": "一"},
        {"host": "two.example"},
    ]}), encoding="utf-8")
    db = Database(tmp_path / "mirror.db")
    probe = ServerProbe(db, popular_path=popular)
    assert probe.seed_if_empty() == 2
    assert db.count_servers() == 2
    # 已有数据时不再重复导入
    assert probe.seed_if_empty() == 0
    db.close()


def test_probe_status_reports_interval(tmp_path):
    db = Database(tmp_path / "mirror.db")
    probe = ServerProbe(db, interval=120, concurrency=4)
    status = probe.status()
    assert status["interval"] == 120 and status["concurrency"] == 4
    assert status["enabled"] is False
    db.close()
