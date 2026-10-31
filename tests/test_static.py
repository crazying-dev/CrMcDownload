"""``/page`` 静态看板：首页 / 资源 / 回落 / 穿越拒绝 / 缺 dist 404 / 缓存头。"""

from __future__ import annotations

import pytest

from server_mirror.static import StaticHandler


@pytest.fixture()
def dist(tmp_path):
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>OffCraft</title>", encoding="utf-8")
    (root / "assets" / "app.a1b2c3.js").write_text("console.log(1)", encoding="utf-8")
    (root / "assets" / "app.css").write_text("body{}", encoding="utf-8")
    return root


def decode(result):
    status, headers, body = result
    return status, headers, body.decode("utf-8")


def test_missing_dist_returns_404(tmp_path):
    handler = StaticHandler(tmp_path / "nope")
    assert handler.available is False
    status, _, body = handler.serve("/page")
    assert status == 404 and body == b"dashboard not built"


def test_index_at_root(dist):
    handler = StaticHandler(dist)
    assert handler.available is True
    for path in ("/page", "/page/"):
        status, headers, body = decode(handler.serve(path))
        assert status == 200
        assert "OffCraft" in body
        assert headers["Cache-Control"] == "no-cache"          # HTML 不缓存
        assert headers["Content-Type"].startswith("text/html")


def test_asset_long_cache(dist):
    handler = StaticHandler(dist)
    status, headers, body = decode(handler.serve("/page/assets/app.a1b2c3.js"))
    assert status == 200 and body == "console.log(1)"
    assert "immutable" in headers["Cache-Control"]
    assert "javascript" in headers["Content-Type"]
    status, headers, _ = decode(handler.serve("/page/assets/app.css"))
    assert "text/css" in headers["Content-Type"]


def test_unknown_route_falls_back_to_index(dist):
    handler = StaticHandler(dist)
    status, headers, body = decode(handler.serve("/page/online"))
    assert status == 200 and "OffCraft" in body
    assert headers["Cache-Control"] == "no-cache"


def test_rejects_path_traversal(dist):
    handler = StaticHandler(dist)
    # 越界访问 dist 之外的文件应回落首页而非泄露内容
    status, _, body = decode(handler.serve("/page/../../secret.txt"))
    assert status == 200 and "OffCraft" in body
    # 反斜杠与空字节被拒绝
    assert handler._read("assets\\..\\index.html") is None
    assert handler._read("a\x00b") is None


def test_missing_asset_falls_back_not_404(dist):
    handler = StaticHandler(dist)
    status, _, body = decode(handler.serve("/page/assets/does-not-exist.js"))
    # 资源命中不了 -> history 回落首页
    assert status == 200 and "OffCraft" in body
