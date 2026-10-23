import json
import tempfile
import unittest
from pathlib import Path

from offcraft.content.loader import (
    LOADER_CONFLICTS,
    LOADER_ROWS,
    loader_note,
    loader_support,
    loader_title,
    mc_sort_key,
)
from offcraft.content.target import (
    CONTENT_SUBDIRS,
    DIR_OTHER,
    DIR_ROOT,
    DIR_VERSION,
    Target,
    target_from_folder,
    target_from_instance,
    target_state,
)
from offcraft.instances import Instance, rename_instance, safe_instance_id


def write_version(folder: Path, *, mc: str = "1.20.1", loader: str = "vanilla") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    raw: dict = {"id": folder.name, "type": "release"}
    if loader == "vanilla":
        raw["mainClass"] = "net.minecraft.client.main.Main"
        raw["clientVersion"] = mc
    else:
        raw["inheritsFrom"] = mc
    if loader == "forge":
        raw["mainClass"] = "cpw.mods.bootstraplauncher.BootstrapLauncher"
        raw["libraries"] = [{"name": "net.minecraftforge:forge:47.0.0"}]
    elif loader == "fabric":
        raw["mainClass"] = "net.fabricmc.loader.impl.launch.knot.KnotClient"
    elif loader == "neoforge":
        raw["libraries"] = [{"name": "net.neoforged:neoforge:21.1.236"}]
    (folder / f"{folder.name}.json").write_text(json.dumps(raw), encoding="utf-8")
    return folder


class LoaderRulesTest(unittest.TestCase):
    def test_support_matrix(self) -> None:
        self.assertEqual(loader_support("vanilla", "1.12.2"), (True, ""))
        self.assertTrue(loader_support("fabric", "1.14")[0])
        self.assertFalse(loader_support("fabric", "1.13.2")[0])
        self.assertFalse(loader_support("neoforge", "1.20.1")[0])
        self.assertTrue(loader_support("neoforge", "1.20.2")[0])
        self.assertTrue(loader_support("neoforge", "1.21.1")[0])
        self.assertTrue(loader_support("forge", "1.12.2")[0])

    def test_snapshot_blocked(self) -> None:
        ok, reason = loader_support("fabric", "26.3", version_type="snapshot")
        self.assertFalse(ok)
        self.assertIn("正式版", reason)

    def test_unknown_loader(self) -> None:
        ok, reason = loader_support("liteloader", "1.12.2")
        self.assertFalse(ok)
        self.assertIn("未知加载器", reason)

    def test_conflicts_are_mutual(self) -> None:
        self.assertIn("neoforge", LOADER_CONFLICTS["forge"])
        self.assertIn("forge", LOADER_CONFLICTS["neoforge"])
        self.assertEqual([row[0] for row in LOADER_ROWS], ["fabric", "neoforge", "forge"])

    def test_notes(self) -> None:
        self.assertIn("NeoForge", loader_note("forge", "1.20.4"))
        self.assertEqual(loader_note("forge", "1.20.1"), "")
        self.assertIn("Fabric API", loader_note("fabric", "1.20.1"))
        self.assertIn("Forge", loader_note("neoforge", "1.21.1"))

    def test_sort_key(self) -> None:
        self.assertLess(mc_sort_key("1.9.4"), mc_sort_key("1.20.1"))
        self.assertLess(mc_sort_key("1.21"), mc_sort_key("1.21.1"))
        self.assertEqual(loader_title("neoforge"), "NeoForge")


class TargetTest(unittest.TestCase):
    def test_subdir(self) -> None:
        target = Target(path=Path("D:/mc"), label="mc")
        self.assertEqual(target.subdir("mod"), Path("D:/mc/mods"))
        self.assertEqual(CONTENT_SUBDIRS["shader"], "shaderpacks")

    def test_from_version_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = write_version(Path(tmp) / "versions" / "1.20.1-forge", loader="forge")
            target = target_from_folder(folder)
            self.assertEqual(target.source, "folder")
            self.assertEqual(target.dir_kind, DIR_VERSION)
            self.assertEqual(target.loader, "Forge")
            self.assertEqual(target.mc_version, "1.20.1")
            self.assertTrue(target.is_game_dir)

    def test_from_game_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_version(root / "versions" / "1.21.1", mc="1.21.1", loader="fabric")
            target = target_from_folder(root)
            self.assertEqual(target.dir_kind, DIR_ROOT)
            self.assertEqual(target.loader, "Fabric")
            self.assertEqual(target.mc_version, "1.21.1")

    def test_from_plain_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = target_from_folder(tmp)
            self.assertEqual(target.dir_kind, DIR_OTHER)
            self.assertFalse(target.is_game_dir)

    def test_from_instance_isolated(self) -> None:
        item = Instance(
            id="1.20.1-fabric",
            loader="Fabric",
            mc_version="1.20.1",
            type="release",
            java_major=17,
            size_bytes=0,
            mtime=0.0,
        )
        isolated = target_from_instance("D:/mc", item)
        shared = target_from_instance("D:/mc", item, isolated=False)
        self.assertEqual(isolated.dir_kind, DIR_VERSION)
        self.assertEqual(shared.dir_kind, DIR_ROOT)
        self.assertNotEqual(isolated.key, shared.key)
        self.assertIn("Fabric", isolated.label)

    def test_state_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            version_dir = write_version(
                Path(tmp) / "versions" / "1.20.1-forge", mc="1.20.1", loader="forge"
            )
            target = target_from_folder(version_dir)
            ok, hint = target_state(target, "mod")
            self.assertTrue(ok)
            self.assertIn("自动创建", hint)
            (version_dir / "mods").mkdir()
            self.assertEqual(target_state(target, "mod"), (True, ""))

    def test_vanilla_instance_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = write_version(Path(tmp) / "versions" / "1.21", mc="1.21")
            target = target_from_folder(folder)
            ok, reason = target_state(target, "mod")
            self.assertFalse(ok)
            self.assertIn("原版实例", reason)
            # 已经有 mods/ 也不放行：原版根本不会加载模组
            (folder / "mods").mkdir()
            self.assertFalse(target_state(target, "mod")[0])
            # 资源包不需要加载器
            self.assertTrue(target_state(target, "resourcepack")[0])

    def test_plain_folder_needs_subdir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = target_from_folder(tmp)
            ok, reason = target_state(target, "resourcepack")
            self.assertFalse(ok)
            self.assertIn("resourcepacks", reason)
            (Path(tmp) / "resourcepacks").mkdir()
            self.assertEqual(target_state(target, "resourcepack"), (True, ""))

    def test_game_root_with_mods(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_version(root / "versions" / "1.21.1", mc="1.21.1", loader="neoforge")
            (root / "mods").mkdir()
            target = target_from_folder(root)
            self.assertEqual(target.loader, "NeoForge")
            self.assertEqual(target_state(target, "mod"), (True, ""))
            ok, hint = target_state(target, "shader")
            self.assertTrue(ok)
            self.assertIn("自动创建", hint)


class RenameTest(unittest.TestCase):
    def test_rename_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_version(root / "versions" / "1.21", mc="1.21")
            new_id, note = rename_instance(root, "1.21", "我的 1.21")
            self.assertEqual(new_id, "我的 1.21")
            self.assertEqual(note, "")
            folder = root / "versions" / "我的 1.21"
            self.assertTrue((folder / "我的 1.21.json").is_file())
            self.assertFalse((root / "versions" / "1.21").exists())
            raw = json.loads((folder / "我的 1.21.json").read_text(encoding="utf-8"))
            self.assertEqual(raw["id"], "我的 1.21")

    def test_rename_blocked_by_inheritance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_version(root / "versions" / "1.21", mc="1.21")
            write_version(root / "versions" / "1.21-forge", mc="1.21", loader="forge")
            new_id, note = rename_instance(root, "1.21", "base")
            self.assertEqual(new_id, "1.21")
            self.assertIn("1.21-forge", note)
            self.assertTrue((root / "versions" / "1.21").is_dir())

    def test_rename_blocked_by_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_version(root / "versions" / "a", mc="1.21")
            write_version(root / "versions" / "b", mc="1.21")
            new_id, note = rename_instance(root, "a", "b")
            self.assertEqual(new_id, "a")
            self.assertIn("已存在", note)

    def test_rename_same_name_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_version(root / "versions" / "a", mc="1.21")
            self.assertEqual(rename_instance(root, "a", "a"), ("a", ""))
            self.assertEqual(rename_instance(root, "a", "  "), ("a", ""))

    def test_safe_instance_id(self) -> None:
        self.assertEqual(safe_instance_id("a/b:c*?"), "a_b_c__")
        self.assertEqual(safe_instance_id("   ", "fallback"), "fallback")
        self.assertEqual(len(safe_instance_id("x" * 80)), 48)
        self.assertEqual(safe_instance_id("  生存  服  "), "生存 服")


if __name__ == "__main__":
    unittest.main()
