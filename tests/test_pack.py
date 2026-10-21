import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from offcraft.install.plan import build_plan
from offcraft.meta.version import load_version
from offcraft.mirror import DirectSource
from offcraft.pack.offline import PackError, export_pack, import_pack, inspect_pack

FIXTURES = Path(__file__).parent / "fixtures"


class PackTest(unittest.TestCase):
    def _make_install(self, root: Path) -> Path:
        version_id = "1.14"
        src = root / "src"
        version_dir = src / "versions" / version_id
        version_dir.mkdir(parents=True)
        (version_dir / f"{version_id}.json").write_text(
            (FIXTURES / "1.14.json").read_text(encoding="utf-8"), encoding="utf-8"
        )
        merged = load_version(src, version_id)
        plan = build_plan(src, version_id, merged.raw, merged, DirectSource())
        for task in plan.tasks:
            task.dest.parent.mkdir(parents=True, exist_ok=True)
            task.dest.write_bytes(b"stub")
        digest = "ab" + "0" * 38
        index = {"objects": {"minecraft/lang/en_us.json": {"hash": digest, "size": 4}}}
        (src / "assets" / "indexes" / "1.14.json").write_text(
            json.dumps(index), encoding="utf-8"
        )
        obj = src / "assets" / "objects" / digest[:2] / digest
        obj.parent.mkdir(parents=True, exist_ok=True)
        obj.write_bytes(b"lang")
        (version_dir / f"{version_id}.jar").write_bytes(b"jar-bytes")
        return src

    def test_export_import_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = self._make_install(root)
            pack = root / "pack.zip"
            info = export_pack(src, "1.14", pack)
            self.assertEqual(info.version_id, "1.14")
            self.assertGreater(info.files, 10)
            meta = inspect_pack(pack)
            self.assertEqual(meta["format"], 1)

            dst = root / "dst"
            result = import_pack(pack, dst)
            self.assertEqual(result.version_id, "1.14")
            self.assertTrue((dst / "versions/1.14/1.14.jar").is_file())
            self.assertEqual((dst / "versions/1.14/1.14.jar").read_bytes(), b"jar-bytes")
            self.assertTrue((dst / "assets/indexes/1.14.json").is_file())

    def test_export_missing_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            version_dir = src / "versions" / "1.14"
            version_dir.mkdir(parents=True)
            (version_dir / "1.14.json").write_text(
                (FIXTURES / "1.14.json").read_text(encoding="utf-8"), encoding="utf-8"
            )
            with self.assertRaises(PackError):
                export_pack(src, "1.14", root / "x.zip")

    def test_reject_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pack = root / "bad.zip"
            with zipfile.ZipFile(pack, "w") as zf:
                zf.writestr("offcraft-pack.json", json.dumps({"format": 1, "version_id": "x"}))
                zf.writestr("../evil.txt", "boom")
            with self.assertRaises(PackError):
                import_pack(pack, root / "dst")
            self.assertFalse((root / "evil.txt").exists())


if __name__ == "__main__":
    unittest.main()
