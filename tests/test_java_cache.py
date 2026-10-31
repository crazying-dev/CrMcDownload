"""启动提速：Java 探测磁盘缓存（命中缓存不 spawn 子进程）。"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from offcraft.launch import java


class JavaCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        # 进程内缓存是模块级全局，逐用例清空避免相互污染
        java._CACHE.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        java._CACHE.clear()
        self._tmp.cleanup()

    def _make_java(self) -> Path:
        exe = self.dir / "bin" / "java.exe"
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_bytes(b"\x00")  # 占位：只为 is_file / stat 成功
        return exe

    def test_cache_hit_skips_subprocess(self) -> None:
        exe = self._make_java()
        mtime = exe.stat().st_mtime
        java.prime_java_cache({str(exe.resolve()).lower(): [mtime, 17]})

        with mock.patch.object(java.subprocess, "run") as run:
            major = java.probe_java(exe)

        self.assertEqual(major, 17)
        run.assert_not_called()

    def test_release_file_avoids_subprocess(self) -> None:
        exe = self._make_java()
        release = exe.parent.parent / "release"
        release.write_text('JAVA_VERSION="1.8.0_301"\n', encoding="utf-8")

        with mock.patch.object(java.subprocess, "run") as run:
            major = java.probe_java(exe)

        run.assert_not_called()
        self.assertEqual(major, 8)

    def test_export_roundtrip(self) -> None:
        exe = self._make_java()
        mtime = exe.stat().st_mtime
        java.prime_java_cache({str(exe.resolve()).lower(): [mtime, 21]})
        exported = java.export_java_cache()
        self.assertIn(str(exe.resolve()).lower(), exported)
        self.assertEqual(exported[str(exe.resolve()).lower()], [mtime, 21])

    def test_stale_cache_triggers_probe(self) -> None:
        exe = self._make_java()
        # 缓存里的 mtime 与磁盘不符 → 视为失效，走实际探测（此处用 release 文件返回版本）
        java.prime_java_cache({str(exe.resolve()).lower(): [exe.stat().st_mtime + 999, 11]})
        release = exe.parent.parent / "release"
        release.write_text('JAVA_VERSION="11.0.2"\n', encoding="utf-8")

        major = java.probe_java(exe)

        self.assertEqual(major, 11)
        # 失效后重新写入缓存，主版本被纠正
        self.assertEqual(java.export_java_cache()[str(exe.resolve()).lower()][1], 11)


if __name__ == "__main__":
    unittest.main()
