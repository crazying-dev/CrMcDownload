"""下载地址信任策略（offcraft.net.trust）的单元测试。

对应 Issue #1：“未知下载主机要有信任策略，并默认拒绝明文 HTTP”。
"""

import unittest

from offcraft.net.trust import (
    TRUSTED_HOSTS,
    check_url,
    host_of,
    is_https,
    is_trusted_host,
)


class TrustPolicyTest(unittest.TestCase):
    def test_mirror_upstreams_are_trusted(self) -> None:
        for host in (
            "piston-meta.mojang.com",
            "libraries.minecraft.net",
            "meta.fabricmc.net",
            "maven.minecraftforge.net",
            "maven.neoforged.net",
            "cdn.modrinth.com",
        ):
            self.assertIn(host, TRUSTED_HOSTS)
            self.assertTrue(is_trusted_host(host))

    def test_common_cdns_are_trusted(self) -> None:
        for host in (
            "github.com",
            "objects.githubusercontent.com",
            "raw.githubusercontent.com",
            "edge.forgecdn.net",
        ):
            self.assertTrue(is_trusted_host(host), host)

    def test_unknown_host_rejected(self) -> None:
        self.assertFalse(is_trusted_host("evil.example"))
        reason = check_url("https://evil.example/mod.jar")
        self.assertIsNotNone(reason)
        self.assertIn("evil.example", reason or "")

    def test_trusted_https_allowed(self) -> None:
        self.assertIsNone(
            check_url("https://cdn.modrinth.com/data/x/versions/1/mod.jar")
        )

    def test_plain_http_rejected_even_when_trusted(self) -> None:
        reason = check_url("http://cdn.modrinth.com/data/x/mod.jar")
        self.assertIsNotNone(reason)
        self.assertIn("HTTP", reason or "")

    def test_untrusted_opt_in_keeps_https_only(self) -> None:
        self.assertIsNone(check_url("https://evil.example/a.jar", allow_untrusted=True))
        self.assertIsNotNone(check_url("http://evil.example/a.jar", allow_untrusted=True))

    def test_bad_inputs(self) -> None:
        self.assertIsNotNone(check_url(""))
        self.assertIsNotNone(check_url("file:///etc/passwd"))
        self.assertIsNotNone(check_url("https://"))
        self.assertEqual(host_of("HTTPS://CDN.Modrinth.com/x"), "cdn.modrinth.com")
        self.assertTrue(is_https("https://cdn.modrinth.com/x"))
        self.assertFalse(is_https("http://cdn.modrinth.com/x"))


if __name__ == "__main__":
    unittest.main()
