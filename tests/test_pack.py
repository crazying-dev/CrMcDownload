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

    def test_traversal_rejected_before_any_write(self) -> None:
        """即使非法成员排在后面，也要在动磁盘之前全部校验完。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pack = root / "bad2.zip"
            with zipfile.ZipFile(pack, "w") as zf:
                zf.writestr("offcraft-pack.json", json.dumps({"format": 1, "version_id": "x"}))
                zf.writestr("ok.txt", "fine")
                zf.writestr("../evil.txt", "boom")
            dst = root / "dst"
            with self.assertRaises(PackError):
                import_pack(pack, dst)
            self.assertFalse((dst / "ok.txt").exists(), "校验失败时不能先写一部分")


class ImportBudgetTest(unittest.TestCase):
    """离线包导入的资源限制与覆盖策略（Issue #1 P2）。"""

    def _pack(self, root: Path, members: dict[str, bytes], **meta) -> Path:
        pack = root / "pack.zip"
        payload = {"format": 1, "version_id": "1.0", "created_at": "2026-01-01"}
        payload.update(meta)
        with zipfile.ZipFile(pack, "w") as zf:
            zf.writestr("offcraft-pack.json", json.dumps(payload))
            for name, data in members.items():
                zf.writestr(name, data)
        return pack

    def test_single_file_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pack = self._pack(root, {"big.bin": b"x" * 4096})
            with self.assertRaises(PackError):
                import_pack(pack, root / "dst", max_file_bytes=1024)
            self.assertFalse((root / "dst" / "big.bin").exists())

    def test_total_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pack = self._pack(root, {f"f{index}.bin": b"y" * 100 for index in range(3)})
            dst = root / "dst"
            with self.assertRaises(PackError):
                import_pack(pack, dst, max_total_bytes=150)
            self.assertFalse((dst / "f0.bin").exists())

    def test_member_count_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pack = self._pack(root, {"a.txt": b"a", "b.txt": b"b"})
            with self.assertRaises(PackError):
                import_pack(pack, root / "dst", max_members=1)

    def test_duplicate_members_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pack = root / "dup.zip"
            with zipfile.ZipFile(pack, "w") as zf:
                zf.writestr("offcraft-pack.json", json.dumps({"format": 1}))
                zf.writestr("same.txt", "one")
                zf.writestr("same.txt", "two")
            with self.assertRaises(PackError):
                import_pack(pack, root / "dst")

    def test_oversized_metadata_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pack = self._pack(root, {"a.txt": b"a"})
            with self.assertRaises(PackError):
                inspect_pack(pack, max_bytes=8)

    def test_conflict_policies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dst = root / "dst"
            first = self._pack(root, {"mods/a.jar": b"original"})
            import_pack(first, dst)
            target = dst / "mods" / "a.jar"
            self.assertEqual(target.read_bytes(), b"original")

            second = self._pack(root, {"mods/a.jar": b"updated!"})
            with self.assertRaises(PackError):
                import_pack(second, dst)
            self.assertEqual(target.read_bytes(), b"original", "error 策略不能改文件")

            info = import_pack(second, dst, on_conflict="skip")
            self.assertEqual(info.skipped, 1)
            self.assertEqual(target.read_bytes(), b"original")

            info = import_pack(second, dst, on_conflict="overwrite")
            self.assertEqual(info.skipped, 0)
            self.assertEqual(target.read_bytes(), b"updated!")

    def test_unknown_conflict_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pack = self._pack(root, {"a.txt": b"a"})
            with self.assertRaises(PackError):
                import_pack(pack, root / "dst", on_conflict="yolo")

    def test_failure_rolls_back_new_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pack = self._pack(root, {"first.txt": b"1", "second.txt": b"2"})
            dst = root / "dst"

            def boom(done: int, total: int, name: str) -> None:
                if name == "second.txt":
                    raise RuntimeError("磁盘写满")

            with self.assertRaises(RuntimeError):
                import_pack(pack, dst, progress=boom)
            self.assertFalse((dst / "first.txt").exists(), "失败后应回滚本次新建的文件")
            self.assertFalse((dst / "second.txt").exists())
            leftovers = list(dst.rglob("*.importing")) if dst.exists() else []
            self.assertEqual(leftovers, [], "不能留下临时文件")



if __name__ == "__main__":
    unittest.main()
