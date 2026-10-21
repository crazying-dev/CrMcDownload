import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from offcraft.install.plan import build_plan
from offcraft.launch.auth import make_profile, offline_uuid
from offcraft.launch.command import LaunchError, LaunchOptions, build_command
from offcraft.launch.java import detect_java
from offcraft.launch.natives import extract_natives
from offcraft.meta.version import load_version
from offcraft.mirror import DirectSource

FIXTURES = Path(__file__).parent / "fixtures"


def _fake_install(root: Path, version_id: str, java_major: int = 25) -> Path:
    version_dir = root / "versions" / version_id
    version_dir.mkdir(parents=True)
    (version_dir / f"{version_id}.json").write_text(
        (FIXTURES / f"{version_id}.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    merged = load_version(root, version_id)
    plan = build_plan(root, version_id, merged.raw, merged, DirectSource())
    for task in plan.tasks:
        task.dest.parent.mkdir(parents=True, exist_ok=True)
        if "-natives" in task.dest.name or "natives" in task.label.lower():
            with zipfile.ZipFile(task.dest, "w") as zf:
                zf.writestr("dummy.dll", b"native")
        else:
            task.dest.write_bytes(b"")
    jar = root / "versions" / merged.jar_id / f"{merged.jar_id}.jar"
    jar.parent.mkdir(parents=True, exist_ok=True)
    jar.write_bytes(b"")
    runtime = root / "runtime" / "java-runtime-epsilon"
    (runtime / "bin").mkdir(parents=True)
    (runtime / "bin" / "java.exe").write_bytes(b"")
    (runtime / "release").write_text(f'JAVA_VERSION="{java_major}.0.1"\n', encoding="utf-8")
    return runtime / "bin" / "java.exe"


class AuthTest(unittest.TestCase):
    def test_known_uuid(self) -> None:
        self.assertEqual(offline_uuid("Notch"), "b50ad385-829d-3141-a216-7e7d7539ba7f")

    def test_validate(self) -> None:
        with self.assertRaises(ValueError):
            make_profile("a")
        with self.assertRaises(ValueError):
            make_profile("bad name")
        profile = make_profile("Alan_123")
        self.assertEqual(profile.access_token, "0")
        self.assertEqual(profile.auth_session, f"token:0:{profile.uuid}")


class CommandTest(unittest.TestCase):
    def test_modern_command(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            java = _fake_install(root, "26.1.2", 25)
            version = load_version(root, "26.1.2")
            profile = make_profile("Player")
            plan = build_command(
                version,
                root,
                profile,
                options=LaunchOptions(memory_mb=4096, java_path=str(java), width=1280, height=720),
            )
            self.assertEqual(str(plan.java.path), str(java))
            argv = plan.argv
            self.assertIn("net.minecraft.client.main.Main", argv)
            self.assertIn("-Xmx4096M", argv)
            self.assertIn("-Xms1024M", argv)
            self.assertIn("--assetIndex", argv)
            self.assertEqual(argv[argv.index("--assetIndex") + 1], "30")
            self.assertEqual(argv[argv.index("--username") + 1], "Player")
            self.assertEqual(argv[argv.index("--uuid") + 1], profile.uuid)
            self.assertEqual(argv[argv.index("--gameDir") + 1], str(root))
            self.assertIn("--width", argv)
            self.assertEqual(argv[argv.index("--width") + 1], "1280")
            classpath = argv[argv.index("-cp") + 1]
            self.assertIn(";", classpath)
            self.assertTrue(classpath.split(";")[-1].endswith("26.1.2.jar"))
            self.assertFalse([arg for arg in argv if "${" in arg])
            self.assertTrue(plan.natives_dir.is_dir())

    def test_legacy_command(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            java = _fake_install(root, "rd-132211", 8)
            version = load_version(root, "rd-132211")
            profile = make_profile("Player")
            plan = build_command(
                version, root, profile, options=LaunchOptions(java_path=str(java))
            )
            argv = plan.argv
            self.assertIn("com.mojang.rubydung.RubyDung", argv)
            main_index = argv.index("com.mojang.rubydung.RubyDung")
            self.assertEqual(argv[main_index + 1], "Player")
            self.assertEqual(argv[main_index + 2], profile.auth_session)
            self.assertIn("-Djava.library.path=" + str(plan.natives_dir), argv)
            self.assertFalse([arg for arg in argv if "${" in arg])

    def test_missing_dependency(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            java = _fake_install(root, "1.14", 8)
            (root / "versions" / "1.14" / "1.14.jar").unlink()
            version = load_version(root, "1.14")
            with self.assertRaises(LaunchError):
                build_command(
                    version, root, make_profile("Player"),
                    options=LaunchOptions(java_path=str(java)),
                )


class NativesTest(unittest.TestCase):
    def test_extract_and_guard(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            jar = root / "natives.jar"
            with zipfile.ZipFile(jar, "w") as zf:
                zf.writestr("test.dll", b"dll")
                zf.writestr("sub/dir/x.dll", b"x")
                zf.writestr("META-INF/MANIFEST.MF", b"manifest")
                zf.writestr("../evil.dll", b"evil")
            written = extract_natives([jar], root / "out")
            self.assertEqual(written, 2)
            self.assertTrue((root / "out" / "test.dll").is_file())
            self.assertTrue((root / "out" / "sub" / "dir" / "x.dll").is_file())
            self.assertFalse((root / "out" / "META-INF").exists())
            self.assertFalse((root / "evil.dll").exists())

    def test_skip_existing(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            jar = root / "natives.jar"
            with zipfile.ZipFile(jar, "w") as zf:
                zf.writestr("a.dll", b"abc")
            out = root / "out"
            out.mkdir()
            (out / "a.dll").write_bytes(b"abc")
            self.assertEqual(extract_natives([jar], out), 0)


class JavaTest(unittest.TestCase):
    def test_component_preference(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            for component, major in (("jre-legacy", 8), ("java-runtime-delta", 21)):
                base = root / "runtime" / component
                (base / "bin").mkdir(parents=True)
                (base / "bin" / "java.exe").write_bytes(b"")
                (base / "release").write_text(f'JAVA_VERSION="{major}.0.2"\n', encoding="utf-8")
            chosen = detect_java(root, 21, preferred_component="java-runtime-delta")
            self.assertEqual(chosen.major, 21)
            self.assertIn("java-runtime-delta", chosen.source)


if __name__ == "__main__":
    unittest.main()
