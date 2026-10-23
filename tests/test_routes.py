import unittest

from server_mirror.routes import BadPath, resolve

UPSTREAMS = {
    "meta": "https://piston-meta.mojang.com",
    "data": "https://piston-data.mojang.com",
    "libs": "https://libraries.minecraft.net",
    "assets": "https://resources.download.minecraft.net",
    "fabric": "https://meta.fabricmc.net",
    "fabricmaven": "https://maven.fabricmc.net",
    "forgemaven": "https://maven.minecraftforge.net",
    "neoforgemaven": "https://maven.neoforged.net",
    "modrinth": "https://api.modrinth.com",
    "modcdn": "https://cdn.modrinth.com",
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

    def test_query_is_preserved(self) -> None:
        route = resolve(
            "/modrinth/v2/search?query=sodium&limit=20&facets=%5B%22x%22%5D",
            UPSTREAMS,
        )
        self.assertEqual(route.prefix, "modrinth")
        self.assertEqual(route.relative, "v2/search")
        self.assertEqual(route.upstream_base, "https://api.modrinth.com")
        self.assertEqual(
            route.upstream_path,
            "/v2/search?query=sodium&limit=20&facets=%5B%22x%22%5D",
        )

    def test_query_changes_cache_key(self) -> None:
        first = resolve("/modrinth/v2/search?query=sodium", UPSTREAMS)
        second = resolve("/modrinth/v2/search?query=lithium", UPSTREAMS)
        self.assertNotEqual(first.cache_key, second.cache_key)
        self.assertTrue(first.cache_key.startswith("modrinth/v2/search.q"))
        self.assertEqual(
            resolve("/modrinth/v2/search", UPSTREAMS).cache_key, "modrinth/v2/search"
        )

    def test_modcdn_file(self) -> None:
        route = resolve("/modcdn/data/abc/versions/1/file.jar", UPSTREAMS)
        self.assertEqual(route.prefix, "modcdn")
        self.assertEqual(route.upstream_base, "https://cdn.modrinth.com")
        self.assertEqual(route.cache_key, "modcdn/data/abc/versions/1/file.jar")
        self.assertEqual(route.upstream_path, "/data/abc/versions/1/file.jar")

    def test_forge_maven_metadata(self) -> None:
        route = resolve("/forgemaven/net/minecraftforge/forge/maven-metadata.xml", UPSTREAMS)
        self.assertEqual(route.prefix, "forgemaven")
        self.assertEqual(route.upstream_base, "https://maven.minecraftforge.net")
        self.assertEqual(route.relative, "net/minecraftforge/forge/maven-metadata.xml")

    def test_neoforge_maven_jar(self) -> None:
        path = "/neoforgemaven/releases/net/neoforged/neoforge/21.1.72/neoforge-21.1.72.jar"
        route = resolve(path, UPSTREAMS)
        self.assertEqual(route.prefix, "neoforgemaven")
        self.assertEqual(route.upstream_base, "https://maven.neoforged.net")
        self.assertEqual(
            route.relative, "releases/net/neoforged/neoforge/21.1.72/neoforge-21.1.72.jar"
        )

    def test_rejects_bad_query(self) -> None:
        with self.assertRaises(BadPath):
            resolve("/modrinth/v2/search?query=a\nb", UPSTREAMS)
        with self.assertRaises(BadPath):
            resolve("/modrinth/v2/search?q=" + "a" * 3000, UPSTREAMS)


if __name__ == "__main__":
    unittest.main()
