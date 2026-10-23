"""缓存容量、淘汰、过期回收与临时文件唯一性（Issue #1 P1/P2）。"""

import os
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from server_mirror.cache import Cache


def _age(path: Path, seconds: float) -> None:
    """把文件 mtime 往前拨，模拟旧缓存。"""
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


class CapacityTest(unittest.TestCase):
    def test_single_file_limit(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = Cache(tmp, max_bytes=1000, max_file_bytes=50)
            self.assertTrue(cache.allows(0))
            self.assertTrue(cache.allows(50))
            self.assertFalse(cache.allows(51))
            self.assertFalse(cache.allows(2000))

    def test_evicts_oldest_until_under_limit(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = Cache(tmp, max_bytes=100)
            paths = []
            for index, name in enumerate(("a", "b", "c")):
                target = cache.path_for(f"libs/{name}.bin")
                tmp_path, fh = cache.begin_write(target)
                fh.write(b"x" * 40)
                fh.close()
                cache.commit(tmp_path, target, size=40)
                _age(target, 100 - index)  # a 最旧
                paths.append(target)
            self.assertLessEqual(cache.total_bytes(refresh=True), 100)
            self.assertFalse(paths[0].exists(), "最旧的条目应被淘汰")
            self.assertTrue(paths[2].exists(), "最新条目必须保留")
            self.assertGreater(cache.evicted_bytes, 0)

    def test_total_bytes_matches_disk(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = Cache(tmp)
            for name in ("x", "y"):
                target = cache.path_for(f"libs/{name}.bin")
                tmp_path, fh = cache.begin_write(target)
                fh.write(b"y" * 10)
                fh.close()
                cache.commit(tmp_path, target, size=10)
            self.assertEqual(cache.total_bytes(), 20)
            self.assertEqual(cache.stats()["bytes"], 20)

    def test_free_space_compresses_to_half(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = Cache(tmp, max_bytes=100)
            for index, name in enumerate(("a", "b", "c")):
                target = cache.path_for(f"libs/{name}.bin")
                tmp_path, fh = cache.begin_write(target)
                fh.write(b"z" * 40)
                fh.close()
                cache.commit(tmp_path, target, size=40)
                _age(target, 100 - index)
            self.assertLessEqual(cache.total_bytes(refresh=True), 100)
            cache.free_space()
            self.assertLessEqual(cache.total_bytes(refresh=True), 50)


class SweepTest(unittest.TestCase):
    def test_sweep_removes_only_expired_metadata(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = Cache(tmp)
            meta = cache.path_for("meta/mc/game/version_manifest_v2.json")
            meta.parent.mkdir(parents=True, exist_ok=True)
            meta.write_bytes(b"{}")
            jar = cache.path_for("libs/a.jar")
            jar.parent.mkdir(parents=True, exist_ok=True)
            jar.write_bytes(b"jar")
            _age(meta, 3600)
            _age(jar, 3600)
            removed = cache.sweep_expired(ttl=600, min_interval=0)
            self.assertGreater(removed, 0)
            self.assertFalse(meta.exists(), "过期元数据应被清掉")
            self.assertTrue(jar.exists(), "jar 路由不算元数据，不能被回收")

    def test_sweep_keeps_fresh_metadata(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = Cache(tmp)
            fresh = cache.path_for("meta/a.json")
            fresh.parent.mkdir(parents=True, exist_ok=True)
            fresh.write_bytes(b"{}")
            jar = cache.path_for("libs/a.jar")
            jar.parent.mkdir(parents=True, exist_ok=True)
            jar.write_bytes(b"jar")
            self.assertEqual(cache.sweep_expired(ttl=600, min_interval=0), 0)
            self.assertTrue(fresh.exists())
            self.assertTrue(jar.exists())

    def test_sweep_is_rate_limited(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = Cache(tmp)
            old = cache.path_for("meta/old.json")
            old.parent.mkdir(parents=True, exist_ok=True)
            old.write_bytes(b"{}")
            _age(old, 3600)
            self.assertGreater(cache.sweep_expired(ttl=600), 0)
            other = cache.path_for("meta/old2.json")
            other.write_bytes(b"{}")
            _age(other, 3600)
            self.assertEqual(
                cache.sweep_expired(ttl=600, min_interval=300), 0, "间隔内不重复扫盘"
            )
            self.assertTrue(other.exists())

    def test_purge_temp_files(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = Cache(tmp)
            target = cache.path_for("libs/a.jar")
            stale = target.with_name(target.name + ".999.1.deadbeef.tmp")
            stale.parent.mkdir(parents=True, exist_ok=True)
            stale.write_bytes(b"half")
            _age(stale, 7200)
            self.assertEqual(cache.purge_temp(max_age=3600), 4)
            self.assertFalse(stale.exists())


class TempFileTest(unittest.TestCase):
    def test_temp_names_are_unique_per_write(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = Cache(tmp)
            target = cache.path_for("libs/a.jar")
            first, fh1 = cache.begin_write(target)
            second, fh2 = cache.begin_write(target)
            try:
                self.assertNotEqual(first, second, "同一进程内两次写入不能共用一个临时文件")
                fh1.write(b"one")
                fh2.write(b"two")
            finally:
                fh1.close()
                fh2.close()
            cache.commit(first, target, size=3)
            other = cache.path_for("libs/b.jar")
            cache.commit(second, other, size=3)
            self.assertEqual(target.read_bytes(), b"one")
            self.assertEqual(other.read_bytes(), b"two")
            leftovers = [p.name for p in target.parent.iterdir() if p.name.endswith(".tmp")]
            self.assertEqual(leftovers, [])

    def test_concurrent_writes_stay_intact(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = Cache(tmp)
            target = cache.path_for("libs/c.jar")
            errors: list[BaseException] = []

            def worker(payload: bytes) -> None:
                try:
                    tmp_path, fh = cache.begin_write(target)
                    with fh:
                        fh.write(payload)
                    cache.commit(tmp_path, target, size=len(payload))
                except BaseException as exc:  # noqa: BLE001 - 汇总给断言
                    errors.append(exc)

            threads = [
                threading.Thread(target=worker, args=(bytes([index]) * 4096,))
                for index in range(6)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            self.assertEqual(len(target.read_bytes()), 4096)
            self.assertEqual(
                [p.name for p in target.parent.iterdir() if p.name.endswith(".tmp")], []
            )


if __name__ == "__main__":
    unittest.main()
