"""客户端皮肤：Pillow 校验、slim/classic 判定、本地缓存与上传（走令牌 origin）。"""

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from offcraft.skins import upload as skin
from offcraft.skins.upload import SkinError, read_skin


def _png(size, right_half_opaque: bool = False) -> bytes:
    from PIL import Image

    image = Image.new("RGBA", size, (0, 0, 0, 0))
    # 左半区（头/身）画点不透明像素，保证是合法皮肤
    for x in range(0, min(8, size[0])):
        for y in range(0, min(8, size[1])):
            image.putpixel((x, y), (120, 60, 30, 255))
    if right_half_opaque:
        # 新版布局右半区（32..64, 0..32）有不透明像素 → slim
        for x in range(32, min(40, size[0])):
            for y in range(0, min(8, size[1])):
                image.putpixel((x, y), (200, 200, 200, 255))
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return buf.getvalue()


class _RecordingDownloader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def request_json(self, url, method="GET", **kwargs):
        self.calls.append((method, url))
        return {"ok": True, "status": "saved"}


class _FakeMirror:
    def __init__(self, base: str) -> None:
        self.base_url = base

    def api_url(self, path: str) -> str:
        return f"{self.base_url}{path}"


class ValidateTests(unittest.TestCase):
    def test_rejects_non_png(self) -> None:
        with self.assertRaises(SkinError):
            skin.validate_png(b"not a png at all")

    def test_rejects_bad_size(self) -> None:
        with self.assertRaises(SkinError):
            skin.validate_png(_png((32, 32)))

    def test_accepts_64x64(self) -> None:
        self.assertEqual(skin.validate_png(_png((64, 64))), (64, 64))


class ModelTests(unittest.TestCase):
    def test_classic_when_right_half_transparent(self) -> None:
        self.assertEqual(skin.detect_model(_png((64, 64), right_half_opaque=False)), "classic")

    def test_slim_when_right_half_opaque(self) -> None:
        self.assertEqual(skin.detect_model(_png((64, 64), right_half_opaque=True)), "slim")

    def test_64x32_always_classic(self) -> None:
        self.assertEqual(skin.detect_model(_png((64, 32))), "classic")


class ReadSaveTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        # 皮肤缓存目录指向临时区，别污染 %APPDATA%
        self._cache_patch = mock.patch.object(
            skin, "skin_cache_dir", lambda: self.dir / "skins"
        )
        self._cache_patch.start()

    def tearDown(self) -> None:
        self._cache_patch.stop()
        self._tmp.cleanup()

    def test_read_skin_auto_detect(self) -> None:
        path = self.dir / "s.png"
        path.write_bytes(_png((64, 64), right_half_opaque=True))
        data, info = read_skin(path, "Notch", model="auto")
        self.assertEqual(info.name, "Notch")
        self.assertEqual(info.model, "slim")
        self.assertEqual(info.filename, "Notch_slim.png")

    def test_invalid_name_rejected(self) -> None:
        path = self.dir / "s.png"
        path.write_bytes(_png((64, 64)))
        with self.assertRaises(SkinError):
            read_skin(path, "bad name!!")

    def test_local_cache_roundtrip(self) -> None:
        data = _png((64, 32))
        info = skin.SkinInfo(name="Steve", model="classic", width=64, height=32, sha1="x", size=len(data))
        written = skin.save_local_skin(info, data)
        self.assertTrue(written.is_file())
        self.assertEqual(skin.load_local_skin("Steve", model="classic"), data)
        self.assertIsNone(skin.load_local_skin("Steve", model="slim"))


class UploadTests(unittest.TestCase):
    def test_upload_targets_mirror_origin(self) -> None:
        data = _png((64, 64))
        info = skin.SkinInfo(name="Steve", model="classic", width=64, height=64, sha1="x", size=len(data))
        downloader = _RecordingDownloader()
        mirror = _FakeMirror("https://mirror.example")
        result = skin.upload_skin(data, info, downloader=downloader, mirror=mirror)
        self.assertEqual(result.get("status"), "saved")
        method, url = downloader.calls[0]
        self.assertEqual(method, "POST")
        # 上传只发给镜像 origin（download_base），不带第三方直连地址
        self.assertTrue(url.startswith("https://mirror.example/skin/Steve.png"))
        self.assertIn("model=classic", url)


if __name__ == "__main__":
    unittest.main()
