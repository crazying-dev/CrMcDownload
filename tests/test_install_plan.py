import unittest
from pathlib import Path

from offcraft.install.plan import InstallOptions, build_plan, maven_path, maven_path_with_classifier
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


if __name__ == "__main__":
    unittest.main()
