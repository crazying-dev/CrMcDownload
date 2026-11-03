"""皮肤托管：Pillow 校验（魔数 / 尺寸 / 大小）、原子写、slim 判定、路径安全。"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from server_mirror.skins import MAX_SKIN_BYTES, SkinError, SkinStore


def _png(size=(64, 64), *, right_opaque=False) -> bytes:
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    # 头部区域（左上 8×8）总是有内容
    for x in range(8):
        for y in range(8):
            image.putpixel((x, y), (10, 20, 30, 255))
    if right_opaque:
        region = Image.new("RGBA", (32, 32), (1, 2, 3, 255))
        image.paste(region, (32, 0), region)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture()
def store(tmp_path):
    return SkinStore(tmp_path / "skins")


# ------------------------------------------------------------- 校验
def test_rejects_non_png(store):
    with pytest.raises(SkinError):
        store.write("Steve", b"GIF89a not a png")


def test_rejects_wrong_size(store):
    with pytest.raises(SkinError):
        store.write("Steve", _png(size=(128, 128)))


def test_rejects_oversized(store):
    with pytest.raises(SkinError):
        store.write("Steve", b"\x89PNG\r\n\x1a\n" + b"\x00" * (MAX_SKIN_BYTES + 1))


def test_accepts_64x64_and_64x32(store):
    model, w, h = store.write("Steve", _png((64, 64)))
    assert (w, h) == (64, 64)
    model, w, h = store.write("Alex", _png((64, 32)))
    assert model == "classic" and (w, h) == (64, 32)


# ---------------------------------------------------------- slim 判定
def test_auto_detects_classic_and_slim(store):
    model, _, _ = store.write("Classic", _png((64, 64), right_opaque=False), model="auto")
    assert model == "classic"
    model, _, _ = store.write("Slim", _png((64, 64), right_opaque=True), model="auto")
    assert model == "slim"


def test_explicit_model_overrides_auto(store):
    model, _, _ = store.write("Forced", _png((64, 64), right_opaque=False), model="slim")
    assert model == "slim"
    assert store.path_for("Forced", slim=True).is_file()


# ---------------------------------------------------------- 路径安全
def test_check_name_rejects_traversal_and_bad_chars(store):
    for bad in ("../etc/passwd", "Steve/Alex", "a b", "", "名", "x" * 17):
        with pytest.raises(SkinError):
            store.check_name(bad)


def test_read_returns_none_for_unknown(store):
    assert store.read("Ghost", slim=False) is None


def test_read_roundtrip(store):
    data = _png((64, 64), right_opaque=True)
    store.write("Steve", data, model="slim")
    assert store.read("Steve", slim=True) == data
    assert store.read("Steve", slim=False) is None      # 只存了 slim


def test_write_is_atomic_no_temp_leftover(store):
    store.write("Steve", _png((64, 64)))
    leftovers = list(store.root.glob("*.importing"))
    assert leftovers == []
    # 覆盖同名文件
    store.write("Steve", _png((64, 32)), model="classic")
    assert store.read("Steve", slim=False) is not None


def test_status_counts_files(store):
    assert store.status()["count"] == 0
    store.write("Steve", _png((64, 64)))
    store.write("Alex", _png((64, 64), right_opaque=True), model="slim")
    status = store.status()
    assert status["count"] == 2
    assert status["last_updated"] > 0


# ------------------------------------------------------- 默认皮肤 / 列表 / 删除
def test_default_skin_is_valid_and_cached():
    from server_mirror.default_skin import default_skin_bytes

    data = default_skin_bytes()
    image = Image.open(io.BytesIO(data))
    assert image.size == (64, 64)
    assert len(data) < MAX_SKIN_BYTES
    assert default_skin_bytes() is default_skin_bytes()      # 进程内缓存同一对象


def test_write_removes_opposite_model(store):
    store.write("Steve", _png((64, 64)))
    assert store.path_for("Steve", slim=False).is_file()
    store.write("Steve", _png((64, 64), right_opaque=True), model="slim")
    assert not store.path_for("Steve", slim=False).exists()
    assert store.path_for("Steve", slim=True).is_file()


def test_delete_removes_both_models(store):
    store.write("Steve", _png((64, 64)))
    store.write("Steve", _png((64, 64), right_opaque=True), model="slim")
    # 直接构造出两套文件，模拟旧数据（正常 write 会互斥）
    store.path_for("Steve", slim=False).write_bytes(_png((64, 64)))
    store.path_for("Steve", slim=True).write_bytes(_png((64, 64)))
    removed = store.delete("Steve")
    assert sorted(removed) == ["Steve.png", "Steve_slim.png"]
    assert store.delete("Steve") == []


def test_list_orders_and_filters(store):
    store.write("Steve", _png((64, 64)))
    store.write("Alex", _png((64, 64)))
    items = store.list()
    assert {item["name"] for item in items} == {"Steve", "Alex"}
    assert all(item["model"] == "classic" for item in items)
    assert store.list(query="st")[0]["name"] == "Steve"
    assert store.list(limit=1) and len(store.list(limit=1)) == 1
