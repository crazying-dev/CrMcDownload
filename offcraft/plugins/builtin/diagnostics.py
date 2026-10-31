"""官方插件示例：连通性自检（capability ``mirror.ping``）。

聚合镜像 origin 的 ``/health``、``/api/stats/overview`` 与皮肤状态，产出一张
「连通性自检」卡片的数据。不新增顶层界面入口——UI 把它挂在设置页底部（见 a10）。

只用受限上下文：所有探测走 ``ctx.probe(相对路径)``，origin 与下载源地址由宿主
解析，插件本身**看不到**具体主机名。
"""

from __future__ import annotations

from offcraft.plugins.base import PluginManifest
from offcraft.plugins.context import PluginContext

__all__ = ["MANIFEST", "DiagnosticsPlugin", "create"]

MANIFEST = PluginManifest(
    id="diagnostics",
    name="连通性自检",
    description="检查下载源与热度榜接口是否连通，并汇总缓存/在线状态。",
    version="1.0.0",
    api=1,
    capabilities=("mirror.ping", "ui.log", "config.namespace"),
    detail="只读探测，不上传任何数据。",
)


class DiagnosticsPlugin:
    def __init__(self) -> None:
        self._ctx: PluginContext | None = None

    # -- 生命周期 ------------------------------------------------------
    def setup(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        ctx.log("INFO", "[diagnostics] 连通性自检插件已加载")

    def teardown(self) -> None:
        self._ctx = None

    # -- 供 UI 调用的取数 ----------------------------------------------
    def report(self) -> list[dict[str, object]]:
        """返回自检行：``{label, ok, detail}``；上下文缺失时给出占位提示。"""
        if self._ctx is None:
            return [{"label": "自检", "ok": False, "detail": "插件未启用"}]
        rows: list[dict[str, object]] = []
        rows.append(self._probe_row("服务健康", "/health", ("status", "cache")))
        rows.append(self._probe_row("统计概览", "/api/stats/overview", ("metrics", "servers")))
        rows.append(self._probe_row("皮肤服务", "/api/skins/status", ("count",)))
        return rows

    def _probe_row(self, label: str, path: str, expect: tuple[str, ...]) -> dict[str, object]:
        assert self._ctx is not None
        try:
            data = self._ctx.probe(path)
        except Exception as exc:  # noqa: BLE001 - 探测失败降级为一行提示
            return {"label": label, "ok": False, "detail": f"无法连通：{exc}"}
        if not isinstance(data, dict):
            return {"label": label, "ok": False, "detail": "响应格式异常"}
        missing = [key for key in expect if key not in data]
        if missing:
            return {"label": label, "ok": False, "detail": f"缺少字段：{', '.join(missing)}"}
        return {"label": label, "ok": True, "detail": self._summarize(path, data)}

    @staticmethod
    def _summarize(path: str, data: dict) -> str:
        if path == "/health":
            cache = data.get("cache") or {}
            files = cache.get("files", "?")
            return f"状态 {data.get('status', '?')}，缓存文件 {files}"
        if path == "/api/stats/overview":
            metrics = data.get("metrics") or {}
            servers = data.get("servers") or {}
            return f"命中 {metrics.get('hits', 0)}/未命中 {metrics.get('misses', 0)}，收录服务器 {servers.get('total', 0)}"
        if path == "/api/skins/status":
            return f"已存皮肤 {data.get('count', 0)} 张"
        return "正常"


def create() -> DiagnosticsPlugin:
    return DiagnosticsPlugin()
