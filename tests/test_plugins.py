"""插件雏形：capability 白名单、命名空间配置、启停持久化、api 版本门控。"""

import sys
import tempfile
import types
import unittest
from pathlib import Path

from offcraft.plugins import base, registry
from offcraft.plugins.base import PLUGIN_API, PluginError, PluginManifest
from offcraft.plugins.context import PluginContext


class _FakeHost:
    """实现 PluginHost 协议的最小宿主（记录调用，供断言）。"""

    def __init__(self) -> None:
        self.notices: list[tuple[str, str]] = []
        self.logs: list[tuple[str, str]] = []
        self.saved = 0
        self.probes: list[str] = []

    def notice(self, text, level="INFO"):
        self.notices.append((text, level))

    def post_log(self, level, text):
        self.logs.append((level, text))

    def register_nav_item(self, plugin_id, label, factory):
        pass

    def run_task(self, name, fn):
        pass

    def list_instances(self):
        return []

    def save_plugin_config(self):
        self.saved += 1

    def mirror_probe(self, path):
        self.probes.append(path)
        return {"status": "ok", "metrics": {}, "servers": {}, "count": 0}

    def mirror_download(self, url, dest):
        return dest


def _context(caps, host=None, store=None):
    return PluginContext("demo", caps, host or _FakeHost(), store if store is not None else {})


class ManifestTests(unittest.TestCase):
    def test_unknown_capability_rejected(self) -> None:
        manifest = PluginManifest(
            id="x", name="X", description="", version="1", api=PLUGIN_API,
            capabilities=("not.a.capability",),
        )
        with self.assertRaises(PluginError):
            manifest.validate()

    def test_higher_api_marked_incompatible(self) -> None:
        manifest = PluginManifest(
            id="x", name="X", description="", version="1", api=PLUGIN_API + 1,
            capabilities=("ui.log",),
        )
        with self.assertRaises(PluginError):
            manifest.validate()


class ContextTests(unittest.TestCase):
    def test_undeclared_capability_denied(self) -> None:
        ctx = _context(("ui.log",))
        with self.assertRaises(PluginError):
            ctx.notice("hi")  # 没声明 ui.notice

    def test_config_namespaced_and_persist(self) -> None:
        host = _FakeHost()
        store: dict = {}
        ctx = _context(("config.namespace",), host=host, store=store)
        ctx.config_set("theme", "dark")
        self.assertEqual(store.get("demo.theme"), "dark")
        self.assertEqual(ctx.config_get("theme"), "dark")
        self.assertEqual(host.saved, 1)  # 写后立即触发落盘

    def test_probe_requires_relative_path(self) -> None:
        host = _FakeHost()
        ctx = _context(("mirror.ping",), host=host)
        self.assertEqual(ctx.probe("/health"), {"status": "ok", "metrics": {}, "servers": {}, "count": 0})
        self.assertEqual(host.probes, ["/health"])
        with self.assertRaises(PluginError):
            ctx.probe("http://evil/host")   # 绝对 URL 被拒（不暴露 origin）
        with self.assertRaises(PluginError):
            ctx.probe("/../secret")          # 穿越被拒


class _DemoPlugin:
    def __init__(self) -> None:
        self.setup_called = 0

    def setup(self, ctx):
        self.setup_called += 1
        ctx.log("INFO", "demo up")

    def teardown(self):
        pass


def _install_fake_module(name: str, manifest: PluginManifest):
    module = types.ModuleType(name)
    module.MANIFEST = manifest
    module.create = lambda: _DemoPlugin()
    sys.modules[name] = module
    return module


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_discover_enable_persist(self) -> None:
        good = _install_fake_module(
            "fake_demo_ok",
            PluginManifest(id="demo", name="Demo", description="", version="1", api=PLUGIN_API,
                           capabilities=("ui.log",), official=True),
        )
        original = registry.BUILTIN_MODULES
        registry.BUILTIN_MODULES = (good.__name__,)
        try:
            reg = registry.PluginRegistry(self.dir, host=_FakeHost())
            reg.load()
            record = reg.get("demo")
            self.assertIsNotNone(record)
            self.assertEqual(record.status, "loaded")   # official 默认启用
            self.assertEqual(record.instance.setup_called, 1)

            self.assertTrue(reg.set_enabled("demo", False))
            self.assertEqual(record.status, "disabled")

            # 重新从磁盘读：启停结论持久化
            fresh = registry.PluginRegistry(self.dir, host=_FakeHost())
            fresh.load()
            self.assertFalse(fresh.get("demo").enabled)
        finally:
            registry.BUILTIN_MODULES = original

    def test_incompatible_skipped(self) -> None:
        bad = _install_fake_module(
            "fake_demo_bad",
            PluginManifest(id="bad", name="Bad", description="", version="1",
                           api=PLUGIN_API + 5, capabilities=("ui.log",)),
        )
        original = registry.BUILTIN_MODULES
        registry.BUILTIN_MODULES = (bad.__name__,)
        try:
            reg = registry.PluginRegistry(self.dir, host=_FakeHost())
            reg.load()
            record = reg.get("bad")
            self.assertEqual(record.status, "incompatible")
            self.assertFalse(record.enabled)
        finally:
            registry.BUILTIN_MODULES = original


if __name__ == "__main__":
    unittest.main()
