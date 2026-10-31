"""插件注册表：仅从内置白名单发现官方插件，启停状态与命名空间配置落盘。

安全边界：

- **import 白名单**：只加载 ``BUILTIN_MODULES`` 列出的模块，绝不扫描/执行第三方目录；
- ``api`` 主版本不匹配 → 标记 ``incompatible`` 且不调用 ``setup``；
- 单个插件 ``setup`` 抛错只记 ``error`` 状态，不影响其他插件与主程序。
"""

from __future__ import annotations

import importlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from offcraft.config import config_dir
from offcraft.plugins.base import PLUGIN_API, Plugin, PluginError, PluginManifest
from offcraft.plugins.context import PluginContext, PluginHost

__all__ = ["PluginRecord", "PluginRegistry"]

#: 官方内置插件模块白名单（新增官方插件在此登记；第三方目录扫描刻意不做）
BUILTIN_MODULES: tuple[str, ...] = (
    "offcraft.plugins.builtin.diagnostics",
)

_STATE_FILE = "plugins.json"


@dataclass(slots=True)
class PluginRecord:
    manifest: PluginManifest
    enabled: bool = False
    #: loaded / disabled / incompatible / error
    status: str = "disabled"
    error: str = ""
    instance: Plugin | None = None
    context: PluginContext | None = None
    nav_items: list[tuple[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.manifest.id,
            "name": self.manifest.name,
            "description": self.manifest.description,
            "version": self.manifest.version,
            "api": self.manifest.api,
            "capabilities": list(self.manifest.capabilities),
            "official": self.manifest.official,
            "detail": self.manifest.detail,
            "enabled": self.enabled,
            "status": self.status,
            "error": self.error,
        }


class PluginRegistry:
    def __init__(self, data_dir: str | Path | None = None, host: PluginHost | None = None) -> None:
        self.data_dir = data_dir
        self.host = host
        self.path = config_dir(data_dir) / _STATE_FILE
        self.records: list[PluginRecord] = []
        self._enabled: dict[str, bool] = {}
        self._config: dict[str, Any] = {}
        self._loaded = False

    # -- 持久化 --------------------------------------------------------
    def _read_state(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if isinstance(data, dict):
            enabled = data.get("enabled") or {}
            self._enabled = {str(k): bool(v) for k, v in enabled.items()}
            config = data.get("config") or {}
            self._config = dict(config) if isinstance(config, dict) else {}

    def save(self) -> None:
        directory = config_dir(self.data_dir)
        directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "enabled": {rec.manifest.id: rec.enabled for rec in self.records} or self._enabled,
            "config": self._config,
        }
        tmp = self.path.with_name(_STATE_FILE + ".tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass

    # -- 发现与加载 ----------------------------------------------------
    def load(self) -> list[PluginRecord]:
        """一次性发现并加载内置插件（幂等）。"""
        if self._loaded:
            return self.records
        self._loaded = True
        self._read_state()
        for module_name in BUILTIN_MODULES:
            try:
                module = importlib.import_module(module_name)
            except Exception as exc:  # noqa: BLE001 - 内置模块坏了也不崩主程序
                self._add_error(module_name, f"模块导入失败：{exc}")
                continue
            manifest = getattr(module, "MANIFEST", None)
            factory = getattr(module, "create", None)
            if not isinstance(manifest, PluginManifest) or not callable(factory):
                self._add_error(module_name, "缺少 MANIFEST 或 create()")
                continue
            self._register(module, manifest, factory)
        return self.records

    def _add_error(self, module_name: str, message: str) -> None:
        stub = PluginManifest(
            id=module_name.rsplit(".", 1)[-1],
            name=module_name,
            description="",
            version="0",
            api=PLUGIN_API,
        )
        self.records.append(PluginRecord(manifest=stub, status="error", error=message))

    def _register(self, module, manifest: PluginManifest, factory) -> None:
        enabled = bool(self._enabled.get(manifest.id, manifest.official))
        try:
            manifest.validate()
        except PluginError as exc:
            self.records.append(
                PluginRecord(manifest=manifest, enabled=False, status="incompatible", error=str(exc))
            )
            return
        try:
            instance = factory()
        except Exception as exc:  # noqa: BLE001
            self.records.append(
                PluginRecord(manifest=manifest, enabled=False, status="error", error=f"实例化失败：{exc}")
            )
            return
        record = PluginRecord(manifest=manifest, instance=instance, enabled=enabled)
        self.records.append(record)
        if enabled and self.host is not None:
            self._setup(record)

    def _setup(self, record: PluginRecord) -> None:
        assert record.instance is not None and self.host is not None
        context = PluginContext(
            record.manifest.id, record.manifest.capabilities, self.host, self._config
        )
        record.context = context
        try:
            record.instance.setup(context)
            record.status = "loaded"
        except Exception as exc:  # noqa: BLE001 - 插件坏了不拖累其他
            record.status = "error"
            record.error = str(exc)

    # -- 启停 ----------------------------------------------------------
    def get(self, plugin_id: str) -> PluginRecord | None:
        for record in self.records:
            if record.manifest.id == plugin_id:
                return record
        return None

    def set_enabled(self, plugin_id: str, enabled: bool) -> bool:
        record = self.get(plugin_id)
        if record is None or record.status == "incompatible":
            return False
        if record.enabled == enabled:
            return True
        record.enabled = enabled
        if enabled and self.host is not None:
            self._setup(record)
        else:
            self._teardown(record)
            record.status = "disabled"
        self.save()
        return True

    def _teardown(self, record: PluginRecord) -> None:
        if record.instance is not None and record.context is not None:
            try:
                record.instance.teardown()
            except Exception:  # noqa: BLE001 - 卸载失败静默
                pass
        record.context = None

    def unload_all(self) -> None:
        for record in self.records:
            self._teardown(record)

    def list(self) -> list[dict[str, Any]]:
        return [record.as_dict() for record in self.records]
