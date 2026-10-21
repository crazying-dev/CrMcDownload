import unittest

from server_mirror.routes import BadPath, resolve

UPSTREAMS = {
    "meta": "https://piston-meta.mojang.com",
    "data": "https://piston-data.mojang.com",
    "libs": "https://libraries.minecraft.net",
    "assets": "https://resources.download.minecraft.net",
    "fabric": "https://meta.fabricmc.net",
}


class RoutesTest(unittest.TestCase):
    def test_meta(self) -> None:
        route = resolve("/meta/mc/game/version_manifest_v2.json", UPSTREAMS)
        self.assertEqual(route.prefix, "meta")
        self.assertEqual(route.relative, "mc/game/version_manifest_v2.json")
        self.assertEqual(route.upstream_base, "https://piston-meta.mojang.com")
        self.assertEqual(route.cache_key, "meta/mc/game/version_manifest_v2.json")

    def test_url_encoded(self) -> None:
        route = resolve("/libs/org/lwjgl/lwjgl/3.3.3/lwjgl-3.3.3.jar", UPSTREAMS)
        self.assertEqual(route.relative, "org/lwjgl/lwjgl/3.3.3/lwjgl-3.3.3.jar")

    def test_rejects_traversal(self) -> None:
        for path in ("/meta/../etc/passwd", "/meta/%2e%2e/etc/passwd", "/meta//x", "/unknown/x"):
            with self.assertRaises(BadPath, msg=path):
                resolve(path, UPSTREAMS)

    def test_rejects_absolute_and_colon(self) -> None:
        with self.assertRaises(BadPath):
            resolve("/meta/C:/windows/win.ini", UPSTREAMS)
        with self.assertRaises(BadPath):
            resolve("meta/x", UPSTREAMS)

    def test_rejects_control_chars(self) -> None:
        with self.assertRaises(BadPath):
            resolve("/meta/x\nz", UPSTREAMS)

    def test_missing_upstream(self) -> None:
        with self.assertRaises(BadPath):
            resolve("/meta/x", {"meta": ""})


if __name__ == "__main__":
    unittest.main()
