"""皮肤路由：默认皮肤、列表、删除与令牌鉴权。"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass

import pytest
from PIL import Image

from server_mirror.api import ApiRouter
from server_mirror.db import Database
from server_mirror.skins import SkinStore


@dataclass
class _Settings:
    token: str = ""


def _png() -> bytes:
    image = Image.new("RGBA", (64, 64), (10, 20, 30, 255))
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture()
def router(tmp_path):
    db = Database(tmp_path / "mirror.db")
    skins = SkinStore(tmp_path / "skins")
    handle = ApiRouter(settings=_Settings(), db=db, skins=skins)
    yield handle, skins
    db.close()


def _json(payload: bytes) -> dict:
    return json.loads(payload.decode("utf-8"))


def test_default_skin_is_served_without_upload(router):
    handle, _ = router
    status, headers, body = handle.dispatch("GET", "/skin/default.png", {}, None)
    assert status == 200 and headers["Content-Type"] == "image/png"
    image = Image.open(io.BytesIO(body))
    assert image.size == (64, 64)


def test_missing_skin_fallback_flag(router):
    handle, _ = router
    status, headers, _ = handle.dispatch("GET", "/skin/ghost.png?fallback=1", {}, None)
    assert status == 200 and headers["X-Skin-Fallback"] == "default"
    status, _, _ = handle.dispatch("GET", "/skin/ghost.png", {}, None)
    assert status == 404


def test_upload_list_delete_roundtrip(router):
    handle, _ = router
    status, _, _ = handle.dispatch("POST", "/skin/Steve.png?model=classic", {}, _png())
    assert status == 200
    status, _, payload = handle.dispatch("GET", "/api/skins/list", {}, None)
    data = _json(payload)
    assert status == 200 and data["count"] == 1
    entry = data["items"][0]
    assert entry["name"] == "Steve" and entry["model"] == "classic"
    assert entry["size"] > 0 and entry["updated"] > 0
    status, _, payload = handle.dispatch("DELETE", "/skin/Steve.png", {}, None)
    assert status == 200 and _json(payload)["removed"] == ["Steve.png"]
    status, _, _ = handle.dispatch("GET", "/skin/Steve.png", {}, None)
    assert status == 404


def test_delete_requires_token_when_configured(tmp_path):
    db = Database(tmp_path / "mirror.db")
    handle = ApiRouter(
        settings=_Settings(token="tok"), db=db, skins=SkinStore(tmp_path / "skins")
    )
    status, _, _ = handle.dispatch("DELETE", "/skin/Steve.png", {}, None)
    assert status == 403
    status, _, _ = handle.dispatch("DELETE", "/skin/Steve.png", {"X-OffCraft-Token": "tok"}, None)
    assert status == 200
    db.close()


def test_bad_skin_name_is_not_served(router):
    handle, _ = router
    status, _, _ = handle.dispatch("GET", "/skin/.hidden.png", {}, None)
    assert status == 404
