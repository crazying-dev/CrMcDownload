import unittest

from offcraft.mirror import HOST_ROUTES, Mirror, UnknownUpstreamError


class MirrorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.mirror = Mirror("https://mc.example.com/")

    def test_rewrite_meta(self) -> None:
        url = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
        self.assertEqual(
            self.mirror.rewrite(url),
            "https://mc.example.com/meta/mc/game/version_manifest_v2.json",
        )

    def test_rewrite_launchermeta(self) -> None:
        url = "https://launchermeta.mojang.com/v1/packages/abc/1.20.json"
        self.assertEqual(
            self.mirror.rewrite(url), "https://mc.example.com/meta/v1/packages/abc/1.20.json"
        )

    def test_rewrite_data(self) -> None:
        url = "https://piston-data.mojang.com/v1/objects/abc/client.jar"
        self.assertEqual(
            self.mirror.rewrite(url), "https://mc.example.com/data/v1/objects/abc/client.jar"
        )
        self.assertEqual(
            self.mirror.rewrite("https://launcher.mojang.com/v1/objects/abc/client.jar"),
            "https://mc.example.com/data/v1/objects/abc/client.jar",
        )

    def test_rewrite_libraries(self) -> None:
        url = "https://libraries.minecraft.net/org/lwjgl/lwjgl/3.3.3/lwjgl-3.3.3.jar"
        self.assertEqual(
            self.mirror.rewrite(url), "https://mc.example.com/libs/org/lwjgl/lwjgl/3.3.3/lwjgl-3.3.3.jar"
        )

    def test_rewrite_assets_and_query(self) -> None:
        url = "https://resources.download.minecraft.net/ab/abcdef?x=1"
        self.assertEqual(
            self.mirror.rewrite(url), "https://mc.example.com/assets/ab/abcdef?x=1"
        )

    def test_rewrite_fabric(self) -> None:
        url = "https://meta.fabricmc.net/v2/versions/loader/1.21.1"
        self.assertEqual(
            self.mirror.rewrite(url), "https://mc.example.com/fabric/v2/versions/loader/1.21.1"
        )

    def test_unknown_host_rejected(self) -> None:
        with self.assertRaises(UnknownUpstreamError):
            self.mirror.rewrite("https://example.org/file.jar")

    def test_invalid_base(self) -> None:
        with self.assertRaises(ValueError):
            Mirror("ftp://mc.example.com")

    def test_host_coverage(self) -> None:
        self.assertEqual(
            set(HOST_ROUTES),
            {
                "piston-meta.mojang.com",
                "launchermeta.mojang.com",
                "piston-data.mojang.com",
                "launcher.mojang.com",
                "libraries.minecraft.net",
                "resources.download.minecraft.net",
                "meta.fabricmc.net",
                "maven.fabricmc.net",
                "api.modrinth.com",
                "cdn.modrinth.com",
            },
        )

    def test_rewrite_fabric_maven(self) -> None:
        url = "https://maven.fabricmc.net/net/fabricmc/fabric-loader/0.16.9/fabric-loader-0.16.9.jar"
        self.assertEqual(
            self.mirror.rewrite(url),
            "https://mc.example.com/fabricmaven/net/fabricmc/fabric-loader/0.16.9/"
            "fabric-loader-0.16.9.jar",
        )

    def test_rewrite_modrinth_api_keeps_query(self) -> None:
        url = "https://api.modrinth.com/v2/search?query=sodium&limit=20"
        self.assertEqual(
            self.mirror.rewrite(url),
            "https://mc.example.com/modrinth/v2/search?query=sodium&limit=20",
        )

    def test_rewrite_modrinth_cdn(self) -> None:
        url = "https://cdn.modrinth.com/data/AANobbMI/versions/abc/sodium.jar"
        self.assertEqual(
            self.mirror.rewrite(url),
            "https://mc.example.com/modcdn/data/AANobbMI/versions/abc/sodium.jar",
        )


if __name__ == "__main__":
    unittest.main()
