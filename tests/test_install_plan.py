import unittest
from pathlib import Path

from offcraft.install.plan import (
    InstallOptions,
    build_plan,
    is_installer_provided,
    library_tasks,
    maven_path,
    maven_path_with_classifier,
)
from offcraft.meta.rules import OsInfo, is_allowed
from offcraft.meta.version import MergedVersion, load_version_file
from offcraft.mirror import Mirror

FIXTURES = Path(__file__).parent / "fixtures"
MIRROR = Mirror("https://mc.example.com")


def _merged(name: str) -> MergedVersion:
    raw = load_version_file(FIXTURES / f"{name}.json")
    return MergedVersion(id=name, raw=raw, chain=(name,))


class MavenPathTest(unittest.TestCase):
    def test_simple(self) -> None:
        self.assertEqual(
            maven_path("org.lwjgl:lwjgl:3.3.3"),
            "org/lwjgl/lwjgl/3.3.3/lwjgl-3.3.3.jar",
        )

    def test_classifier_and_ext(self) -> None:
        self.assertEqual(
            maven_path("net.java.jinput:jinput-platform:2.0.5:natives-windows"),
            "net/java/jinput/jinput-platform/2.0.5/jinput-platform-2.0.5-natives-windows.jar",
        )
        self.assertEqual(
            maven_path("com.example:demo:1.0@zip"),
            "com/example/demo/1.0/demo-1.0.zip",
        )

    def test_replace_classifier(self) -> None:
        self.assertEqual(
            maven_path_with_classifier("org.lwjgl:lwjgl:3.2.1:natives-linux", "natives-windows"),
            "org/lwjgl/lwjgl/3.2.1/lwjgl-3.2.1-natives-windows.jar",
        )


class BuildPlanTest(unittest.TestCase):
    def test_modern_plan(self) -> None:
        version = _merged("26.1.2")
        plan = build_plan(Path("C:/mc"), "26.1.2", version.raw, version, MIRROR, os_info=OsInfo())
        self.assertIsNotNone(plan.client_jar)
        self.assertEqual(plan.client_jar, Path("C:/mc/versions/26.1.2/26.1.2.jar"))
        self.assertEqual(plan.asset_index_id, "30")
        # 所有任务都指向镜像，绝不直连
        for task in plan.tasks:
            self.assertTrue(task.url.startswith("https://mc.example.com/"), task.url)
        # 允许的库数量与规则过滤一致（artifact + natives）
        allowed = [lib for lib in version.libraries if is_allowed(lib.get("rules"), OsInfo())]
        natives = [lib for lib in allowed if (lib.get("natives") or {}).get("windows")]
        library_tasks = [t for t in plan.tasks if "libraries" in t.dest.parts]
        self.assertEqual(len(library_tasks), len(allowed) + len(natives))
        self.assertIn("assets index 30", [t.label for t in plan.tasks])

    def test_old_version_has_natives(self) -> None:
        version = _merged("1.14")
        plan = build_plan(Path("C:/mc"), "1.14", version.raw, version, MIRROR)
        natives = [t for t in plan.tasks if "natives-windows" in t.label]
        self.assertTrue(natives, "1.14 应包含 natives-windows 分类器")
        for task in natives:
            self.assertIn("libraries", task.dest.parts)

    def test_skip_assets_option_keeps_index(self) -> None:
        version = _merged("1.14")
        plan = build_plan(
            Path("C:/mc"), "1.14", version.raw, version, MIRROR,
            options=InstallOptions(include_assets=False),
        )
        self.assertIn("assets index 1.14", [t.label for t in plan.tasks])
        self.assertFalse([t for t in plan.tasks if "objects" in t.dest.parts])

    def test_classic_version_without_arguments(self) -> None:
        version = _merged("rd-132211")
        plan = build_plan(Path("C:/mc"), "rd-132211", version.raw, version, MIRROR)
        self.assertIsNotNone(plan.client_jar)
        self.assertTrue(plan.tasks)


class LibraryTasksTest(unittest.TestCase):
    """加载器依赖：安装器生成的库要跳过，其余按坐标 / 默认仓库拼 URL。"""

    def test_installer_provided_is_detected(self) -> None:
        patched = {
            "name": "net.minecraftforge:forge:26.2-65.1.5:client",
            "downloads": {
                "artifact": {
                    "path": "net/minecraftforge/forge/26.2-65.1.5/forge-26.2-65.1.5-client.jar",
                    "url": "",
                    "sha1": "be81e6d24785e467d30f30bcf31c18733233d3fe",
                    "size": 79191305,
                }
            },
        }
        self.assertTrue(is_installer_provided(patched))
        self.assertFalse(
            is_installer_provided(
                {
                    "name": "net.minecraftforge:forge:26.2-65.1.5:universal",
                    "downloads": {
                        "artifact": {
                            "path": "net/minecraftforge/forge/26.2-65.1.5/forge-26.2-65.1.5-universal.jar",
                            "url": "https://maven.minecraftforge.net/x.jar",
                        }
                    },
                }
            )
        )
        self.assertFalse(is_installer_provided({"name": "a:b:1.0"}))

    def test_patched_client_is_skipped(self) -> None:
        libraries = [
            {
                "name": "net.minecraftforge:forge:26.2-65.1.5:client",
                "downloads": {
                    "artifact": {
                        "path": "net/minecraftforge/forge/26.2-65.1.5/forge-26.2-65.1.5-client.jar",
                        "url": "",
                        "sha1": "be81e6d24785e467d30f30bcf31c18733233d3fe",
                        "size": 79191305,
                    }
                },
            },
            {
                "name": "net.minecraftforge:forgespi:8.0.0",
                "downloads": {
                    "artifact": {
                        "path": "net/minecraftforge/forgespi/8.0.0/forgespi-8.0.0.jar",
                        "url": "https://maven.minecraftforge.net/net/minecraftforge/forgespi/8.0.0/forgespi-8.0.0.jar",
                        "sha1": "abc",
                        "size": 10,
                    }
                },
            },
        ]
        tasks, warnings = library_tasks(libraries, Path("C:/mc"), MIRROR)
        self.assertEqual([task.dest.name for task in tasks], ["forgespi-8.0.0.jar"])
        self.assertTrue(any("安装器生成" in text for text in warnings))
        self.assertTrue(tasks[0].url.startswith("https://mc.example.com/forgemaven/"))

    def test_maven_default_repo_for_forge_and_neoforge(self) -> None:
        libraries = [
            {"name": "net.minecraftforge:fmlcore:26.2-65.1.5"},
            {"name": "net.neoforged:neoform:1.0"},
            {"name": "com.example:demo:1.0"},
        ]
        tasks, _warnings = library_tasks(libraries, Path("C:/mc"), MIRROR, rewrite=False)
        self.assertEqual(
            tasks[0].url, "https://maven.minecraftforge.net/net/minecraftforge/fmlcore/26.2-65.1.5/fmlcore-26.2-65.1.5.jar"
        )
        self.assertEqual(
            tasks[1].url, "https://maven.neoforged.net/releases/net/neoforged/neoform/1.0/neoform-1.0.jar"
        )
        self.assertEqual(
            tasks[2].url, "https://libraries.minecraft.net/com/example/demo/1.0/demo-1.0.jar"
        )

    def test_duplicate_dest_is_deduped(self) -> None:
        library = {
            "name": "net.minecraftforge:forge:26.2-65.1.5:universal",
            "downloads": {
                "artifact": {
                    "path": "net/minecraftforge/forge/26.2-65.1.5/forge-26.2-65.1.5-universal.jar",
                    "url": "https://maven.minecraftforge.net/u.jar",
                    "sha1": "abc",
                    "size": 1,
                }
            },
        }
        tasks, _warnings = library_tasks([library, dict(library)], Path("C:/mc"), MIRROR)
        self.assertEqual(len(tasks), 1)


if __name__ == "__main__":
    unittest.main()
