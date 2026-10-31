"""GET /plugin/* —— 官方特权插件按需下载的服务端静态路由（免令牌、只读、防穿越）。"""

import json
from dataclasses import dataclass
from pathlib import Path

from server_mirror.api import ApiRouter
from server_mirror.db import Database


@dataclass
class _Settings:
    token: str = ""
    plugin_dir: Path | None = None


def _router(tmp_path, plugin_dir=None):
    db = Database(tmp_path / "mirror.db")
    return ApiRouter(settings=_Settings(plugin_dir=plugin_dir), db=db), db


def _seed(tmp_path):
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    (plugin_dir / "offline-multiplayer.manifest.json").write_text(
        json.dumps({"sha256": "abcd", "abi": "cp312-win_amd64", "size": 7}), encoding="utf-8")
    (plugin_dir / "offline-multiplayer.zip").write_bytes(b"ZIPDATA")
    return plugin_dir


def test_serve_manifest_and_zip(tmp_path):
    plugin_dir = _seed(tmp_path)
    router, db = _router(tmp_path, plugin_dir)
    try:
        status, headers, body = router.dispatch("GET", "/plugin/offline-multiplayer.manifest.json", {})
        assert status == 200
        assert json.loads(body)["sha256"] == "abcd"

        status, headers, body = router.dispatch("GET", "/plugin/offline-multiplayer.zip", {})
        assert status == 200
        assert body == b"ZIPDATA"
        assert headers["Content-Type"] == "application/zip"
    finally:
        db.close()


def test_zip_served_without_token(tmp_path):
    # 读接口免令牌：即便配置了全局令牌也不拦 GET /plugin
    plugin_dir = _seed(tmp_path)
    db = Database(tmp_path / "mirror.db")
    router = ApiRouter(settings=_Settings(token="secret", plugin_dir=plugin_dir), db=db)
    try:
        status, _, _ = router.dispatch("GET", "/plugin/offline-multiplayer.zip", {})
        assert status == 200
    finally:
        db.close()


def test_unknown_plugin_404(tmp_path):
    plugin_dir = _seed(tmp_path)
    router, db = _router(tmp_path, plugin_dir)
    try:
        status, _, _ = router.dispatch("GET", "/plugin/whatever.zip", {})
        assert status == 404
    finally:
        db.close()


def test_traversal_rejected(tmp_path):
    plugin_dir = _seed(tmp_path)
    router, db = _router(tmp_path, plugin_dir)
    try:
        status, _, _ = router.dispatch("GET", "/plugin/../secret", {})
        assert status == 400
    finally:
        db.close()


def test_missing_file_404(tmp_path):
    router, db = _router(tmp_path, tmp_path / "empty-plugins")
    try:
        status, _, _ = router.dispatch("GET", "/plugin/offline-multiplayer.zip", {})
        assert status == 404
    finally:
        db.close()


def test_not_configured_404(tmp_path):
    router, db = _router(tmp_path, None)
    try:
        status, _, _ = router.dispatch("GET", "/plugin/offline-multiplayer.zip", {})
        assert status == 404
    finally:
        db.close()


def test_post_not_allowed(tmp_path):
    plugin_dir = _seed(tmp_path)
    router, db = _router(tmp_path, plugin_dir)
    try:
        status, _, _ = router.dispatch("POST", "/plugin/offline-multiplayer.zip", {})
        assert status == 405
    finally:
        db.close()
