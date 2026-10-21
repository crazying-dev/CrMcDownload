import json
import tempfile
import unittest
from pathlib import Path

from offcraft.config import Config, config_dir, load_config, save_config
from offcraft.instances import detect_loader, discover_instances


class ConfigTest(unittest.TestCase):
    def test_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(download_base="https://mc.example.com/", username="Steve", memory_mb=8192)
            path = save_config(config, tmp)
            self.assertTrue(path.is_file())
            loaded = load_config(tmp)
            self.assertEqual(loaded.username, "Steve")
            self.assertEqual(loaded.memory_mb, 8192)
            self.assertEqual(loaded.download_base, "https://mc.example.com")

    def test_corrupt_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = config_dir(tmp)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "config.json").write_text("{not json", encoding="utf-8")
            loaded = load_config(tmp)
            self.assertEqual(loaded.username, "Player")
            backups = list(directory.glob("config.corrupt-*.json"))
            self.assertEqual(len(backups), 1)

    def test_version_override(self) -> None:
        config = Config(memory_mb=4096, version_overrides={"1.20": {"memory_mb": 8192}})
        merged = config.apply_version_overrides("1.20")
        self.assertEqual(merged["memory_mb"], 8192)
        self.assertEqual(config.apply_version_overrides("1.21")["memory_mb"], 4096)


class InstancesTest(unittest.TestCase):
    def test_discover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            version_dir = root / "versions" / "1.20.1-forge"
            version_dir.mkdir(parents=True)
            (version_dir / "1.20.1-forge.json").write_text(
                json.dumps(
                    {
                        "id": "1.20.1-forge",
                        "inheritsFrom": "1.20.1",
                        "mainClass": "cpw.mods.bootstraplauncher.BootstrapLauncher",
                        "javaVersion": {"majorVersion": 17},
                        "libraries": [{"name": "net.minecraftforge:forge:47.0.0"}],
                    }
                ),
                encoding="utf-8",
            )
            items = discover_instances(root)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].loader, "Forge")
            self.assertEqual(items[0].mc_version, "1.20.1")
            self.assertEqual(items[0].java_major, 17)

    def test_loader_detection(self) -> None:
        self.assertEqual(detect_loader("net.fabricmc.loader.impl.launch.knot.KnotClient", []), "Fabric")
        self.assertEqual(
            detect_loader("cpw.mods.bootstraplauncher.BootstrapLauncher",
                          [{"name": "net.neoforged:neoforge:21.1.236"}]), "NeoForge"
        )
        self.assertEqual(detect_loader("net.minecraft.client.main.Main", []), "原版")


if __name__ == "__main__":
    unittest.main()
