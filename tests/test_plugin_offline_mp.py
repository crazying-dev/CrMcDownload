"""离线联机插件化：net.tunnel 仅官方、下载包 sha256 完整性、注册表按已下载 bundle 加载。

全程禁网：下载产物用内存 zip 直接喂给 bundle.install_zip；注册表扫描临时 data_dir 下的
插件目录，验证「校验通过才 import、篡改即拒绝加载」。
"""

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from offcraft.plugins import bundle, registry
from offcraft.plugins.base import PLUGIN_API, PluginError, PluginManifest
from offcraft.plugins.bundle import BundleError
from offcraft.plugins.context import PluginContext

_PLUGIN_ID = "offline-multiplayer"

_MANIFEST_JSON = json.dumps(
    {"id": _PLUGIN_ID, "name": "离线联机", "entry": "plugin_main.py",
     "capabilities": ["net.tunnel", "ui.log", "config.namespace"], "official": True},
    ensure_ascii=False,
)

_PLUGIN_MAIN = '''
from offcraft.plugins.base import PLUGIN_API, PluginManifest


MANIFEST = PluginManifest(
    id="offline-multiplayer", name="离线联机", description="", version="1.0.0",
    api=PLUGIN_API, capabilities=("net.tunnel", "ui.log", "config.namespace"),
    official=True,
)


class _Plugin:
    def __init__(self):
        self.setup_called = 0
        self.torndown = 0

    def setup(self, ctx):
        self.setup_called += 1

    def teardown(self):
        self.torndown += 1


def create():
    return _Plugin()
'''


def _make_zip(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buffer.getvalue()


class _FakeHost:
    def notice(self, text, level="INFO"):
        pass

    def post_log(self, level, text):
        pass

    def register_nav_item(self, plugin_id, label, factory):
        pass

    def run_task(self, name, fn):
        pass

    def list_instances(self):
        return []

    def save_plugin_config(self):
        pass

    def mirror_probe(self, path):
        return {}

    def mirror_download(self, url, dest):
        return dest


class CapabilityGateTest(unittest.TestCase):
    def test_net_tunnel_official_only(self):
        good = PluginManifest(id="mp", name="MP", description="", version="1", api=PLUGIN_API,
                              capabilities=("net.tunnel",), official=True)
        good.validate()  # 官方可声明
        bad = PluginManifest(id="mp", name="MP", description="", version="1", api=PLUGIN_API,
                             capabilities=("net.tunnel",), official=False)
        with self.assertRaises(PluginError):
            bad.validate()

    def test_context_tunnel_requires_capability(self):
        ctx = PluginContext("mp", ("ui.log",), _FakeHost(), {})
        with self.assertRaises(PluginError):
            ctx.p2p_open_tunnel()
        granted = PluginContext("mp", ("net.tunnel",), _FakeHost(), {})
        self.assertTrue(granted.p2p_open_tunnel())


class BundleIntegrityTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_install_verify_and_manifest(self):
        data = _make_zip({"manifest.json": _MANIFEST_JSON, "plugin_main.py": _PLUGIN_MAIN})
        digest = bundle.sha256_bytes(data)
        directory = bundle.install_zip(self.dir, data, expected_sha256=digest, abi="cp312-win_amd64")
        self.assertTrue(bundle.is_installed(self.dir))
        self.assertTrue(bundle.verify_bundle(self.dir))
        self.assertEqual(bundle.read_manifest(self.dir)["id"], _PLUGIN_ID)
        self.assertTrue((directory / "plugin_main.py").is_file())

    def test_wrong_sha256_rejected(self):
        data = _make_zip({"manifest.json": _MANIFEST_JSON, "plugin_main.py": _PLUGIN_MAIN})
        with self.assertRaises(BundleError):
            bundle.install_zip(self.dir, data, expected_sha256="0" * 64)
        self.assertFalse(bundle.is_installed(self.dir))

    def test_missing_manifest_rejected(self):
        data = _make_zip({"plugin_main.py": _PLUGIN_MAIN})
        with self.assertRaises(BundleError):
            bundle.install_zip(self.dir, data, expected_sha256=bundle.sha256_bytes(data))

    def test_post_install_tamper_fails_verify(self):
        data = _make_zip({"manifest.json": _MANIFEST_JSON, "plugin_main.py": _PLUGIN_MAIN})
        bundle.install_zip(self.dir, data, expected_sha256=bundle.sha256_bytes(data))
        self.assertTrue(bundle.verify_bundle(self.dir))
        # 落盘后替换入口文件 → 逐文件校验必须失败
        (bundle.bundle_dir(self.dir) / "plugin_main.py").write_text("import os\n", encoding="utf-8")
        self.assertFalse(bundle.verify_bundle(self.dir))


class RegistryDownloadTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        data = _make_zip({"manifest.json": _MANIFEST_JSON, "plugin_main.py": _PLUGIN_MAIN})
        bundle.install_zip(self.dir, data, expected_sha256=bundle.sha256_bytes(data))

    def tearDown(self):
        registry.PluginRegistry(self.dir, host=_FakeHost()).reload()  # 清缓存
        self._tmp.cleanup()

    def _registry(self):
        reg = registry.PluginRegistry(self.dir, host=_FakeHost())
        reg.reload()
        return reg

    def test_downloaded_bundle_discovered_disabled(self):
        reg = self._registry()
        record = reg.get(_PLUGIN_ID)
        self.assertIsNotNone(record)
        self.assertIn("net.tunnel", record.manifest.capabilities)
        self.assertTrue(record.manifest.official)
        self.assertEqual(record.status, "disabled")  # 下载后默认不自动启用
        self.assertFalse(record.enabled)

    def test_enable_loads_official_plugin(self):
        reg = self._registry()
        self.assertTrue(reg.set_enabled(_PLUGIN_ID, True))
        record = reg.get(_PLUGIN_ID)
        self.assertEqual(record.status, "loaded")
        self.assertEqual(record.instance.setup_called, 1)

    def test_tampered_bundle_refused(self):
        (bundle.bundle_dir(self.dir) / "plugin_main.py").write_text("raise RuntimeError('bad')\n",
                                                                    encoding="utf-8")
        reg = self._registry()
        record = reg.get(_PLUGIN_ID)
        self.assertIsNotNone(record)
        self.assertEqual(record.status, "error")
        self.assertFalse(record.enabled)


if __name__ == "__main__":
    unittest.main()
