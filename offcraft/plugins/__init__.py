"""插件接口雏形：仅官方内置插件 + capability 白名单，禁动态加载第三方。

对外只暴露稳定契约，UI 与主程序通过 :class:`PluginRegistry` 装载、查询官方插件。
"""

from __future__ import annotations

from offcraft.plugins.base import (
    CAPABILITIES,
    PLUGIN_API,
    Plugin,
    PluginError,
    PluginManifest,
)
from offcraft.plugins.context import PluginContext, PluginHost
from offcraft.plugins.registry import PluginRecord, PluginRegistry

__all__ = [
    "CAPABILITIES",
    "PLUGIN_API",
    "Plugin",
    "PluginContext",
    "PluginError",
    "PluginHost",
    "PluginManifest",
    "PluginRecord",
    "PluginRegistry",
]
