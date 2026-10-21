import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from server_mirror.cache import Cache, is_metadata


class CacheTest(unittest.TestCase):
    def test_path_and_freshness(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = Cache(tmp)
            path = cache.path_for("meta/a.json")
            self.assertEqual(path, Path(tmp).resolve() / "meta" / "a.json")
            path.parent.mkdir(parents=True)
            path.write_bytes(b"x")
            self.assertTrue(cache.is_fresh(path, ttl=60))
            self.assertTrue(cache.is_fresh(path, ttl=60, immutable=True))
            old = time.time() - 3600
            import os

            os.utime(path, (old, old))
            self.assertFalse(cache.is_fresh(path, ttl=60))
            self.assertTrue(cache.is_fresh(path, ttl=60, immutable=True))

    def test_rejects_escape(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = Cache(tmp)
            with self.assertRaises(ValueError):
                cache.path_for("../evil")

    def test_atomic_write(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = Cache(tmp)
            target = cache.path_for("libs/a.jar")
            tmp_path, fh = cache.begin_write(target)
            fh.write(b"data")
            fh.close()
            self.assertFalse(target.exists())
            cache.commit(tmp_path, target)
            self.assertEqual(target.read_bytes(), b"data")

    def test_is_metadata(self) -> None:
        self.assertTrue(is_metadata("meta"))
        self.assertTrue(is_metadata("fabric"))
        self.assertFalse(is_metadata("libs"))


if __name__ == "__main__":
    unittest.main()
