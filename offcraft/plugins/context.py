"""插件运行上下文：把宿主能力包成白名单接口递给插件。

插件**永远拿不到**原始 ``MainWindow``、任意文件写或裸网络接口。每个访问器都先
检查插件在清单里声明过的 capability，越权即 :class:`~offcraft.plugins.base.PluginError`。
宿主的原语由 :class:`PluginHost` 约定，UI 层（a10）实现一个适配器注入。
"""

from __future__ import annotations

from typing import Any, Callable, Protocol, runtime_checkable

from offcraft.net.trust import check_url
from offcraft.plugins.base import PluginError

__all__ = ["PluginContext", "PluginHost"]


@runtime_checkable
class PluginHost(Protocol):
    """宿主需要实现的最小原语集（与 MainWindow 解耦，便于测试与替换）。"""

    def notice(self, text: str, level: str = "INFO") -> None: ...

    def post_log(self, level: str, text: str) -> None: ...

    def register_nav_item(self, plugin_id: str, label: str, factory: Callable[[], Any]) -> None: ...

    def run_task(self, name: str, fn: Callable[[Any], Any]) -> None: ...

    def list_instances(self) -> list[Any]: ...

    def save_plugin_config(self) -> None: ...

    def mirror_probe(self, path: str) -> dict: ...

    def mirror_download(self, url: str, dest: Any) -> Any: ...


class PluginContext:
    """单个插件的受限视图。``store`` 是命名空间化后的键值映射（由 registry 持有并落盘）。"""

    def __init__(
        self,
        plugin_id: str,
        capabilities: tuple[str, ...] | list[str],
        host: PluginHost,
        store: dict[str, Any],
    ) -> None:
        self._id = plugin_id
        self._caps = frozenset(capabilities)
        self._host = host
        self._store = store
        self._dirty = False

    # -- 自省 -----------------------------------------------------------
    @property
    def plugin_id(self) -> str:
        return self._id

    def capabilities(self) -> list[str]:
        return sorted(self._caps)

    def _require(self, capability: str) -> None:
        if capability not in self._caps:
            raise PluginError(f"插件 {self._id} 未声明能力 '{capability}'，调用被拒")

    # -- 命名空间配置 --------------------------------------------------
    def _namespaced(self, key: str) -> str:
        if not key or key.startswith(".") or ".." in key:
            raise PluginError(f"非法配置键：{key!r}")
        return f"{self._id}.{key}"

    def config_get(self, key: str, default: Any = None) -> Any:
        self._require("config.namespace")
        return self._store.get(self._namespaced(key), default)

    def config_set(self, key: str, value: Any) -> None:
        self._require("config.namespace")
        self._store[self._namespaced(key)] = value
        self._dirty = True
        self._host.save_plugin_config()

    def flush(self) -> bool:
        """registry 在插件切换启停用此判断是否需要落盘（保留 dirty 语义）。"""
        dirty, self._dirty = self._dirty, False
        return dirty

    # -- UI -------------------------------------------------------------
    def notice(self, text: str, level: str = "INFO") -> None:
        self._require("ui.notice")
        self._host.notice(text, level)

    def log(self, level: str, text: str) -> None:
        self._require("ui.log")
        self._host.post_log(level, text)

    def register_nav_item(self, label: str, factory: Callable[[], Any]) -> None:
        self._require("ui.nav_item")
        self._host.register_nav_item(self._id, label, factory)

    # -- 运行时 --------------------------------------------------------
    def register_task(self, fn: Callable[[Any], Any], name: str = "") -> None:
        self._require("runtime.task")
        self._host.run_task(name or self._id, fn)

    def instance_list(self) -> list[Any]:
        self._require("instance.list")
        return list(self._host.list_instances())

    # -- 网络（强护栏）-------------------------------------------------
    def download_file(self, url: str, dest: Any) -> Any:
        """只允许受信/镜像地址，且经宿主镜像改写后下载（不给裸网络接口）。"""
        self._require("net.mirror_fetch")
        checked = check_url(url)
        if checked is None:
            raise PluginError(f"插件下载被拒：{url} 非受信地址")
        return self._host.mirror_download(checked, dest)

    def probe(self, path: str) -> dict:
        """诊断能力：对镜像 origin 的相对 API 路径取 JSON。

        插件只给相对路径（如 ``/health``、``/api/stats/overview``），origin 由
        宿主解析并校验——下载源地址对插件同样不暴露。
        """
        self._require("mirror.ping")
        if not path.startswith("/") or "//" in path or ".." in path:
            raise PluginError(f"非法探测路径：{path!r}")
        return self._host.mirror_probe(path)
