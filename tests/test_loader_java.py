"""Java 运行时与加载器：纯函数 / 清单解析层测试（不联网）。"""

import tempfile
import unittest
from pathlib import Path

from offcraft.content import java as java_mod
from offcraft.content.java import JavaRuntime, JavaRuntimeError, list_java_runtimes
from offcraft.content.loader import (
    LOADERS,
    LoaderError,
    _neoforge_prefix,
    install_loader,
    list_loader_versions,
    supports_mods,
)
from offcraft.mirror import Mirror

MIRROR = Mirror("https://mc.example.com")


class _FakeDownloader:
    """按 URL 片段返回固定内容，用于解析层测试。"""

    def __init__(self, json_map=None, bytes_map=None) -> None:
        self.json_map = json_map or {}
        self.bytes_map = bytes_map or {}
        self.calls: list[str] = []

    def fetch_json(self, url: str):
        self.calls.append(url)
        for key, value in self.json_map.items():
            if key in url:
                return value
        raise RuntimeError(f"unexpected url {url}")

    def fetch_bytes(self, url: str) -> bytes:
        self.calls.append(url)
        for key, value in self.bytes_map.items():
            if key in url:
                return value
        raise RuntimeError(f"unexpected url {url}")


JAVA_MANIFEST = {
    "windows-x64": {
        "java-runtime-delta": [
            {
                "manifest": {"url": "https://launchermeta.mojang.com/x/delta.json", "size": 3},
                "version": {"name": "21.0.5", "released": "2024-10-15"},
            }
        ],
        "jre-legacy": [
            {
                "manifest": {"url": "https://launchermeta.mojang.com/x/legacy.json"},
                "version": {"name": "8u352", "released": "2022-07-01"},
            }
        ],
        "minecraft-java-exe": [{"manifest": {"url": "https://x/ignored.json"}}],
    }
}


class SupportsModsTest(unittest.TestCase):
    def test_loader_matrix(self) -> None:
        for loader in ("Fabric", "Forge", "NeoForge", "Quilt"):
            self.assertTrue(supports_mods(loader), loader)
        for loader in ("原版", "vanilla", "", "OptiFine"):
            self.assertFalse(supports_mods(loader), loader)

    def test_loader_choices(self) -> None:
        keys = [key for key, _text in LOADERS]
        self.assertEqual(keys, ["vanilla", "fabric", "forge", "neoforge"])


class JavaRuntimeTest(unittest.TestCase):
    def test_major_from_table_and_version(self) -> None:
        self.assertEqual(JavaRuntime("java-runtime-delta", "21.0.5", "", "u").major, 21)
        self.assertEqual(JavaRuntime("custom-runtime", "17.0.9", "", "u").major, 17)
        self.assertEqual(JavaRuntime("custom-runtime", "weird", "", "u").major, 0)

    def test_paths(self) -> None:
        runtime = JavaRuntime("jre-legacy", "8u352", "", "u")
        self.assertEqual(runtime.label, "Java 8 · jre-legacy")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(runtime.installed(tmp))
            target = runtime.java_path(tmp)
            target.parent.mkdir(parents=True)
            target.write_text("", encoding="utf-8")
            self.assertTrue(runtime.installed(tmp))
            self.assertEqual(runtime.install_dir(tmp), Path(tmp) / "runtime" / "jre-legacy")

    def test_list_runtimes_filters_and_sorts(self) -> None:
        downloader = _FakeDownloader(json_map={"all.json": JAVA_MANIFEST})
        runtimes = list_java_runtimes(MIRROR, downloader)
        self.assertEqual([item.component for item in runtimes], ["jre-legacy", "java-runtime-delta"])
        self.assertEqual([item.major for item in runtimes], [8, 21])
        self.assertTrue(downloader.calls[0].startswith("https://mc.example.com/meta/"))

    def test_list_runtimes_empty_raises(self) -> None:
        downloader = _FakeDownloader(json_map={"all.json": {"windows-x64": {"x": []}}})
        with self.assertRaises(JavaRuntimeError):
            list_java_runtimes(MIRROR, downloader)

    def test_safe_rel_blocks_traversal(self) -> None:
        self.assertIsNone(java_mod._safe_rel("../evil.txt"))
        self.assertIsNone(java_mod._safe_rel("/etc/passwd"))
        self.assertIsNone(java_mod._safe_rel("C:/windows/system32"))
        self.assertIsNone(java_mod._safe_rel(""))
        self.assertEqual(java_mod._safe_rel("bin/java.exe"), Path("bin") / "java.exe")


class LoaderVersionsTest(unittest.TestCase):
    def test_neoforge_prefix(self) -> None:
        self.assertEqual(_neoforge_prefix("1.20.4"), "20.4.")
        self.assertEqual(_neoforge_prefix("1.21"), "21.0.")
        self.assertEqual(_neoforge_prefix("1.21.1"), "21.1.")
        self.assertEqual(_neoforge_prefix(""), "")

    def test_fabric_versions(self) -> None:
        payload = (
            b'[{"loader": {"version": "0.16.9", "stable": true}}, '
            b'{"loader": {"version": "0.16.10", "stable": true}}]'
        )
        downloader = _FakeDownloader(bytes_map={"loader/1.21.1": payload})
        builds = list_loader_versions("fabric", "1.21.1", mirror=MIRROR, downloader=downloader)
        self.assertEqual([item.version for item in builds], ["0.16.9", "0.16.10"])
        self.assertIn("mc.example.com/fabric/", downloader.calls[0])

    def test_forge_versions_filtered_and_sorted(self) -> None:
        xml = (
            b"<metadata><versioning><versions>"
            b"<version>1.20.1-47.2.0</version>"
            b"<version>1.20.1-47.4.28</version>"
            b"<version>1.19.2-43.2.0</version>"
            b"</versions></versioning></metadata>"
        )
        downloader = _FakeDownloader(bytes_map={"maven-metadata.xml": xml})
        builds = list_loader_versions("forge", "1.20.1", mirror=MIRROR, downloader=downloader)
        self.assertEqual([item.version for item in builds], ["1.20.1-47.4.28", "1.20.1-47.2.0"])

    def test_vanilla_and_unknown(self) -> None:
        downloader = _FakeDownloader()
        self.assertEqual(list_loader_versions("vanilla", "1.21", mirror=MIRROR, downloader=downloader), [])
        with self.assertRaises(LoaderError):
            list_loader_versions("liteloader", "1.21", mirror=MIRROR, downloader=downloader)

    def test_install_loader_vanilla_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = install_loader(tmp, "1.21.1", "vanilla", "", mirror=MIRROR, downloader=_FakeDownloader())
        self.assertEqual(result, "1.21.1")


if __name__ == "__main__":
    unittest.main()
