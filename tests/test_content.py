"""内容层测试：Modrinth 解析、搜索参数、文件安装与整合包解析（全部离线）。"""

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from offcraft.content.install import ContentInstallError, install_project_version, safe_filename
from offcraft.content.modpack import (
    ModpackError,
    read_mrpack,
    sanitize_instance_name,
    unique_instance_name,
)
from offcraft.content.modrinth import (
    Dependency,
    ModrinthClient,
    ProjectVersion,
    VersionFile,
)
from offcraft.net.downloader import DownloadError

MIRROR = "http://mirror.test"


class FakeDownloader:
    """记录 URL、以内存响应代替网络请求的下载器替身。"""

    def __init__(self, responses: dict | None = None, fail_prefixes: tuple[str, ...] = ()) -> None:
        self.responses = responses or {}
        self.fail_prefixes = fail_prefixes
        self.urls: list[str] = []
        self.written: list = []

    def fetch_json(self, url: str):
        self.urls.append(url)
        for prefix in self.fail_prefixes:
            if url.startswith(prefix):
                raise DownloadError(f"blocked {url}")
        for key, value in self.responses.items():
            if key in url:
                return value
        return {}

    def download(self, task, cancel=None):  # noqa: ANN001, ARG002
        self.written.append(task)
        task.dest.parent.mkdir(parents=True, exist_ok=True)
        task.dest.write_bytes(b"payload")
        return task


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


def _version(version_id: str, filename: str, *, dependencies=(), sha512: str = "") -> ProjectVersion:
    return ProjectVersion(
        id=version_id,
        project_id="project-" + version_id,
        name=version_id,
        version_number=version_id,
        version_type="release",
        game_versions=("1.20.1",),
        loaders=("fabric",),
        files=(
            VersionFile(
                url=f"https://cdn.modrinth.com/data/x/versions/{version_id}/{filename}",
                filename=filename,
                primary=True,
                sha512=sha512,
            ),
        ),
        dependencies=tuple(dependencies),
    )


def _make_pack(folder: Path, index: dict, extra: dict[str, bytes] | None = None) -> Path:
    path = folder / "pack.mrpack"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("modrinth.index.json", json.dumps(index))
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return path


class ModrinthSearchTest(unittest.TestCase):
    def test_search_facets_and_paging(self) -> None:
        fake = FakeDownloader(
            {
                "/search": {
                    "hits": [
                        {
                            "project_id": "AANobbMI",
                            "slug": "sodium",
                            "title": "Sodium",
                            "description": "渲染优化",
                            "project_type": "mod",
                            "downloads": 1234,
                        }
                    ],
                    "total_hits": 107,
                }
            }
        )
        client = ModrinthClient("", downloader=fake)
        result = client.search(
            "sodium", "mod", game_version="1.20.1", loader="fabric", limit=20, offset=40, index="downloads"
        )
        self.assertEqual(result.total, 107)
        self.assertEqual(result.offset, 40)
        self.assertEqual(result.hits[0].slug, "sodium")
        self.assertEqual(result.hits[0].page_url, "https://modrinth.com/mod/sodium")

        url = fake.urls[0]
        self.assertTrue(url.startswith("https://api.modrinth.com/v2/search?"))
        query = _query(url)
        self.assertEqual(query["offset"], ["40"])
        self.assertEqual(query["limit"], ["20"])
        self.assertEqual(query["index"], ["downloads"])
        self.assertEqual(
            json.loads(query["facets"][0]),
            [["project_type:mod"], ["categories:fabric"], ["versions:1.20.1"]],
        )

    def test_loader_facet_ignored_for_resourcepack(self) -> None:
        fake = FakeDownloader({"/search": {"hits": [], "total_hits": 0}})
        client = ModrinthClient("", downloader=fake)
        client.search("", "resourcepack", loader="fabric")
        facets = json.loads(_query(fake.urls[0])["facets"][0])
        self.assertEqual(facets, [["project_type:resourcepack"]])

    def test_mirror_fallback_to_official(self) -> None:
        fake = FakeDownloader(
            {"/search": {"hits": [], "total_hits": 0}}, fail_prefixes=(MIRROR + "/modrinth",)
        )
        client = ModrinthClient(MIRROR, downloader=fake)
        client.search("sodium")
        self.assertEqual(len(fake.urls), 2)
        self.assertTrue(fake.urls[0].startswith(MIRROR + "/modrinth/v2/search?"))
        self.assertTrue(fake.urls[1].startswith("https://api.modrinth.com/v2/search?"))
        self.assertEqual(client.api_mode, "官方源")
        self.assertTrue(any("回退" in note for note in client.notes))

    def test_resolve_url_rewrites_only_in_mirror_mode(self) -> None:
        fake = FakeDownloader({"/search": {"hits": [], "total_hits": 0}})
        client = ModrinthClient(MIRROR, downloader=fake)
        client.search("sodium")
        self.assertEqual(
            client.resolve_url("https://cdn.modrinth.com/data/a/b.jar"),
            MIRROR + "/modcdn/data/a/b.jar",
        )
        direct = ModrinthClient("", downloader=FakeDownloader())
        self.assertEqual(
            direct.resolve_url("https://cdn.modrinth.com/data/a/b.jar"),
            "https://cdn.modrinth.com/data/a/b.jar",
        )

    def test_version_primary_file_and_label(self) -> None:
        version = ProjectVersion.from_json(
            {
                "id": "v1",
                "project_id": "p1",
                "version_number": "1.2.3",
                "version_type": "beta",
                "game_versions": ["1.20.1"],
                "loaders": ["fabric"],
                "files": [
                    {"url": "https://cdn.modrinth.com/extra.jar", "filename": "extra.jar"},
                    {
                        "url": "https://cdn.modrinth.com/main.jar",
                        "filename": "main.jar",
                        "primary": True,
                        "hashes": {"sha1": "aa", "sha512": "bb"},
                    },
                ],
                "dependencies": [{"project_id": "dep", "dependency_type": "required"}],
            }
        )
        self.assertEqual(version.label, "1.2.3")
        self.assertEqual(version.primary_file.filename, "main.jar")
        self.assertEqual(version.primary_file.sha512, "bb")
        self.assertEqual(version.dependencies[0].project_id, "dep")
        self.assertTrue(version.dependencies[0].required)


class InstallTest(unittest.TestCase):
    def test_safe_filename(self) -> None:
        self.assertEqual(safe_filename("a/b:c?d*.jar"), "a_b_c_d_.jar")
        self.assertEqual(safe_filename("   "), "download.bin")

    def test_installs_primary_and_required_dependency(self) -> None:
        fake = FakeDownloader(
            {
                "/version": [
                    {
                        "id": "dep-v1",
                        "project_id": "dep-proj",
                        "version_number": "9.9",
                        "files": [
                            {
                                "url": "https://cdn.modrinth.com/dep.jar",
                                "filename": "dep.jar",
                                "primary": True,
                            }
                        ],
                    }
                ]
            }
        )
        client = ModrinthClient("", downloader=fake)
        version = _version(
            "v1", "main.jar", dependencies=(Dependency(project_id="dep-proj"),)
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = install_project_version(
                client, version, Path(tmp) / "mods", game_version="1.20.1", loader="fabric"
            )
            self.assertEqual(sorted(path.name for path in result.files), ["dep.jar", "main.jar"])
            self.assertTrue((Path(tmp) / "mods" / "main.jar").is_file())
            self.assertTrue((Path(tmp) / "mods" / "dep.jar").is_file())
        self.assertEqual(len(fake.written), 2)

    def test_sha512_mismatch_removes_file(self) -> None:
        fake = FakeDownloader()
        client = ModrinthClient("", downloader=fake)
        version = _version("v1", "bad.jar", sha512="deadbeef")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ContentInstallError):
                install_project_version(client, version, Path(tmp))
            self.assertFalse((Path(tmp) / "bad.jar").exists())

    def test_dependency_cycle_is_bounded(self) -> None:
        fake = FakeDownloader()
        client = ModrinthClient("", downloader=fake)
        version = _version("v1", "a.jar", dependencies=(Dependency(version_id="v1"),))
        with tempfile.TemporaryDirectory() as tmp:
            result = install_project_version(client, version, Path(tmp))
        self.assertEqual(len(result.files), 1)


class ModpackTest(unittest.TestCase):
    def test_sanitize_instance_name(self) -> None:
        self.assertEqual(sanitize_instance_name("a/b:c"), "a_b_c")
        self.assertEqual(sanitize_instance_name("con"), "con-pack")
        self.assertEqual(sanitize_instance_name("   "), "modpack")
        self.assertLessEqual(len(sanitize_instance_name("x" * 200)), 48)

    def test_unique_instance_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "versions" / "pack").mkdir(parents=True)
            (Path(tmp) / "versions" / "pack-2").mkdir()
            self.assertEqual(unique_instance_name(tmp, "pack"), "pack-3")
            self.assertEqual(unique_instance_name(tmp, "other"), "other")

    def test_read_mrpack(self) -> None:
        index = {
            "formatVersion": 1,
            "name": "Fabulously Optimized",
            "versionId": "5.4.0",
            "summary": "性能整合包",
            "dependencies": {"minecraft": "1.20.1", "fabric-loader": "0.16.9"},
            "files": [
                {
                    "path": "mods/sodium.jar",
                    "downloads": ["https://cdn.modrinth.com/data/x/sodium.jar"],
                    "hashes": {"sha1": "aa", "sha512": "bb"},
                    "fileSize": 1024,
                    "env": {"client": "required"},
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = _make_pack(Path(tmp), index, {"overrides/config/a.txt": b"hi"})
            info = read_mrpack(path)
        self.assertEqual(info.name, "Fabulously Optimized")
        self.assertEqual(info.version_id, "5.4.0")
        self.assertEqual(info.mc_version, "1.20.1")
        self.assertEqual(info.loader, "fabric")
        self.assertEqual(info.loader_version, "0.16.9")
        self.assertEqual(info.loader_label, "Fabric")
        self.assertEqual(info.files[0].path, "mods/sodium.jar")
        self.assertEqual(info.files[0].size, 1024)
        self.assertEqual(info.files[0].sha1, "aa")

    def test_read_mrpack_rejects_path_traversal(self) -> None:
        index = {
            "dependencies": {"minecraft": "1.20.1", "fabric-loader": "0.16.9"},
            "files": [
                {
                    "path": "../evil.jar",
                    "downloads": ["https://cdn.modrinth.com/evil.jar"],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = _make_pack(Path(tmp), index)
            with self.assertRaises(ModpackError):
                read_mrpack(path)

    def test_read_mrpack_requires_index_and_minecraft(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "broken.mrpack"
            with zipfile.ZipFile(broken, "w") as archive:
                archive.writestr("readme.txt", "nope")
            with self.assertRaises(ModpackError):
                read_mrpack(broken)

            no_mc = _make_pack(Path(tmp), {"dependencies": {"fabric-loader": "0.16.9"}})
            with self.assertRaises(ModpackError):
                read_mrpack(no_mc)


if __name__ == "__main__":
    unittest.main()
