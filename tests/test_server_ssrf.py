"""联机：服务器地址解析与 SSRF 安全护栏（全部离线，不触发 DNS）。"""

import unittest
from unittest import mock

from offcraft.servers import ssrf
from offcraft.servers.ssrf import (
    DEFAULT_PORT,
    ServerAddressError,
    format_host_port,
    is_public_ip,
    parse_server,
    resolve_is_public,
)


class ParseTests(unittest.TestCase):
    def test_plain_host_and_ip(self) -> None:
        self.assertEqual(parse_server("mc.example.com"), ("mc.example.com", DEFAULT_PORT))
        self.assertEqual(parse_server("MC.example.COM."), ("mc.example.com", DEFAULT_PORT))
        self.assertEqual(parse_server("1.2.3.4"), ("1.2.3.4", DEFAULT_PORT))
        self.assertEqual(parse_server("1.2.3.4:25599"), ("1.2.3.4", 25599))

    def test_ipv6_literal(self) -> None:
        self.assertEqual(parse_server("[::1]:25566"), ("::1", 25566))
        self.assertEqual(parse_server("[2001:db8::1]"), ("2001:db8::1", DEFAULT_PORT))

    def test_rejects_urls_and_credentials(self) -> None:
        for text in (
            "http://mc.example.com",
            "//mc.example.com",
            "user@mc.example.com",
            "mc.example.com/path",
            "mc.example.com:25565/motd",
        ):
            with self.assertRaises(ServerAddressError, msg=text):
                parse_server(text)

    def test_rejects_whitespace_and_empty(self) -> None:
        for text in ("", "   ", "mc.example.com 1", "mc .example.com"):
            with self.assertRaises(ServerAddressError, msg=text):
                parse_server(text)

    def test_rejects_bad_hostnames(self) -> None:
        for host in ("-bad.example.com", "bad-.example.com", "a" * 300 + ".com", "mc_ex\u00e4mples"):
            with self.assertRaises(ServerAddressError, msg=host):
                parse_server(host)

    def test_rejects_bad_ports(self) -> None:
        for text in ("mc.example.com:0", "mc.example.com:70000", "mc.example.com:abc"):
            with self.assertRaises(ServerAddressError, msg=text):
                parse_server(text)


class FormatTests(unittest.TestCase):
    def test_default_port_omitted(self) -> None:
        self.assertEqual(format_host_port("mc.example.com", DEFAULT_PORT), "mc.example.com")

    def test_non_default_port_kept(self) -> None:
        self.assertEqual(format_host_port("mc.example.com", 25566), "mc.example.com:25566")

    def test_ipv6_bracketed(self) -> None:
        self.assertEqual(format_host_port("::1", 25566), "[::1]:25566")
        self.assertEqual(format_host_port("::1", DEFAULT_PORT), "[::1]")


class IpClassifyTests(unittest.TestCase):
    def test_public_literals_allowed(self) -> None:
        for ip in ("8.8.8.8", "1.1.1.1", "2606:4700::1"):
            self.assertTrue(is_public_ip(ip), ip)
            allowed, reason = resolve_is_public(ip)
            self.assertTrue(allowed, reason)

    def test_private_and_special_rejected(self) -> None:
        for ip in (
            "127.0.0.1",
            "10.0.0.5",
            "172.16.0.1",
            "192.168.1.1",
            "169.254.1.1",
            "100.64.0.1",  # CGNAT
            "0.0.0.0",
            "224.0.0.1",  # multicast
            "::1",
            "fe80::1",  # link-local
            "fc00::1",  # ULA
        ):
            self.assertFalse(is_public_ip(ip), ip)
            allowed, reason = resolve_is_public(ip)
            self.assertFalse(allowed, ip)
            self.assertTrue(reason, ip)

    def test_dns_rebinding_blocked_when_resolves_private(self) -> None:
        # 主机名解析到私网必须被拒（不真的走 DNS，打桩 getaddrinfo）
        infos = [(2, 1, 6, "", ("192.168.0.10", 25565))]
        with mock.patch.object(ssrf.socket, "getaddrinfo", return_value=infos):
            allowed, reason = resolve_is_public("evil.example.com")
        self.assertFalse(allowed)
        self.assertIn("内网", reason)

    def test_hostname_resolving_public_allowed(self) -> None:
        infos = [(2, 1, 6, "", ("93.184.216.34", 25565))]
        with mock.patch.object(ssrf.socket, "getaddrinfo", return_value=infos):
            allowed, reason = resolve_is_public("mc.example.com")
        self.assertTrue(allowed)
        self.assertEqual(reason, "")

    def test_unresolvable_host_rejected(self) -> None:
        with mock.patch.object(ssrf.socket, "getaddrinfo", side_effect=OSError("no name")):
            allowed, reason = resolve_is_public("nope.invalid")
        self.assertFalse(allowed)
        self.assertIn("无法解析", reason)


if __name__ == "__main__":
    unittest.main()
