import json
import tempfile
import unittest
from pathlib import Path

from offcraft.meta.manifest import VersionManifest
from offcraft.meta.version import (
    VersionError,
    load_version,
    load_version_file,
    merge_versions,
)

FIXTURES = Path(__file__).parent / "fixtures"


class RealVersionTest(unittest.TestCase):
    def test_modern_version(self) -> None:
        version = load_version_file(FIXTURES / "26.1.2.json")
        self.assertEqual(version["mainClass"], "net.minecraft.client.main.Main")
        self.assertEqual(version["javaVersion"]["majorVersion"], 25)
        self.assertEqual(len(version["libraries"]), 107)
        self.assertEqual(version["assets"], "30")
        self.assertIn("arguments", version)
        self.assertNotIn("inheritsFrom", version)

    def test_old_version_uses_minecraft_arguments(self) -> None:
        version = load_version_file(FIXTURES / "1.14.json")
        self.assertEqual(version["javaVersion"]["majorVersion"], 8)
        self.assertTrue(any("natives" in lib for lib in version["libraries"]))

    def test_classic_version(self) -> None:
        version = load_version_file(FIXTURES / "rd-132211.json")
        self.assertEqual(version["mainClass"], "com.mojang.rubydung.RubyDung")
        self.assertIn("${auth_player_name}", version["minecraftArguments"])
        self.assertNotIn("arguments", version)
        self.assertEqual(version["assets"], "pre-1.6")


class MergeTest(unittest.TestCase):
    def _write(self, root: Path, name: str, data: dict) -> None:
        folder = root / "versions" / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")

    def test_inherits_from(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(
                root,
                "1.20.1",
                {
                    "id": "1.20.1",
                    "type": "release",
                    "mainClass": "net.minecraft.client.main.Main",
                    "assets": "5",
                    "javaVersion": {"majorVersion": 17},
                    "libraries": [{"name": "base:lib:1.0"}],
                    "arguments": {"game": ["--gameDir", "${game_directory}"], "jvm": ["-cp", "${classpath}"]},
                },
            )
            self._write(
                root,
                "1.20.1-forge",
                {
                    "id": "1.20.1-forge",
                    "inheritsFrom": "1.20.1",
                    "mainClass": "cpw.mods.bootstraplauncher.BootstrapLauncher",
                    "libraries": [{"name": "forge:loader:47.0.0"}],
                    "arguments": {"game": ["--launchTarget", "forgeclient"], "jvm": ["-Dfoo=bar"]},
                },
            )
            merged = load_version(root, "1.20.1-forge")
            self.assertEqual(merged.main_class, "cpw.mods.bootstraplauncher.BootstrapLauncher")
            self.assertEqual([lib["name"] for lib in merged.libraries], ["base:lib:1.0", "forge:loader:47.0.0"])
            self.assertEqual(merged.arguments["game"][0], "--gameDir")
            self.assertIn("--launchTarget", merged.arguments["game"])
            self.assertEqual(merged.java_major, 17)
            self.assertEqual(merged.assets, "5")
            self.assertEqual(merged.chain, ("1.20.1", "1.20.1-forge"))

    def test_cycle_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "a", {"id": "a", "inheritsFrom": "b"})
            self._write(root, "b", {"id": "b", "inheritsFrom": "a"})
            with self.assertRaises(VersionError):
                load_version(root, "a")

    def test_missing_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "child", {"id": "child", "inheritsFrom": "nope"})
            with self.assertRaises(VersionError):
                load_version(root, "child")

    def test_merge_keeps_parent_scalars(self) -> None:
        merged = merge_versions(
            {"id": "p", "assets": "5", "libraries": [1], "arguments": {"game": ["a"]}},
            {"id": "c", "assets": "6", "libraries": [2], "arguments": {"game": ["b"]}},
        )
        self.assertEqual(merged["assets"], "6")
        self.assertEqual(merged["libraries"], [1, 2])
        self.assertEqual(merged["arguments"]["game"], ["a", "b"])


class ManifestTest(unittest.TestCase):
    def test_parse_and_filter(self) -> None:
        manifest = VersionManifest.from_json(
            {
                "latest": {"release": "1.21.1", "snapshot": "24w33a"},
                "versions": [
                    {"id": "1.21.1", "type": "release", "url": "https://x/1.21.1.json"},
                    {"id": "24w33a", "type": "snapshot", "url": "https://x/24w33a.json"},
                ],
            }
        )
        self.assertEqual(manifest.latest_release, "1.21.1")
        self.assertEqual(manifest.find("1.21.1").type, "release")
        self.assertIsNone(manifest.find("nope"))
        self.assertEqual([v.id for v in manifest.filter({"snapshot"})], ["24w33a"])
        self.assertEqual([v.id for v in manifest.filter(query="1.21")], ["1.21.1"])


if __name__ == "__main__":
    unittest.main()
