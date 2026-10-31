"""插件契约：清单、接口与错误。仅官方内置插件（capability 白名单）。

设计约束（见架构文档「插件模型」）：

- 只加载随启动器发布的官方插件，**禁止动态加载第三方代码**（防任意执行）；
- 插件通过 :class:`~offcraft.plugins.context.PluginContext` 访问宿主能力，且
  只能用 ``CAPABILITIES`` 白名单声明过的能力；越权调用被拒；
- ``api`` 主版本与 :data:`PLUGIN_API` 不符即标记 ``incompatible`` 并跳过加载。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - 仅类型标注
    from offcraft.plugins.context import PluginContext

__all__ = [
    "CAPABILITIES",
    "PLUGIN_API",
    "Plugin",
    "PluginError",
    "PluginManifest",
]

#: 当前插件宿主 API 的主版本；不兼容变更时 +1
PLUGIN_API = 1

#: 允许官方插件声明的能力（白名单；未列出的能力一律不可得）
CAPABILITIES: frozenset[str] = frozenset(
    {
        "config.namespace",   # 读写 plugins.<id>.* 命名空间
        "ui.notice",          # 向用户弹提示
        "ui.log",             # 写运行日志
        "ui.nav_item",        # 注册导航项（页面工厂）
        "runtime.task",       # 提交后台任务
        "instance.list",      # 只读实例列表
        "net.mirror_fetch",   # 经镜像改写 + trust 校验的下载
        "mirror.ping",        # 诊断：连通性探测聚合
    }
)


class PluginError(RuntimeError):
    """插件清单非法或越权访问宿主能力。"""


@dataclass(frozen=True, slots=True)
class PluginManifest:
    """插件元信息（由插件模块提供，registry 只信任内置清单）。"""

    id: str
    name: str
    description: str
    version: str
    api: int
    #: 声明需要的能力；必须是 :data:`CAPABILITIES` 的子集，否则加载即报错
    capabilities: tuple[str, ...] = ()
    #: 模块内挂载入口（约定名，registry 用 import 白名单而非任意路径）
    entrypoint: str = "PLUGIN"
    official: bool = True
    #: 额外说明（供 UI 卡片展示）
    detail: str = ""

    def validate(self) -> None:
        if not self.id or not self.name:
            raise PluginError("插件缺少 id 或 name")
        if self.api != PLUGIN_API:
            raise PluginError(
                f"插件 {self.id} 需要 API {self.api}，当前宿主为 {PLUGIN_API}"
            )
        unknown = set(self.capabilities) - set(CAPABILITIES)
        if unknown:
            raise PluginError(
                f"插件 {self.id} 声明了未授权能力：{', '.join(sorted(unknown))}"
            )


@runtime_checkable
class Plugin(Protocol):
    """插件实例接口。"""

    manifest: PluginManifest

    def setup(self, ctx: "PluginContext") -> None:
        """加载时被调用一次；异常由 registry 捕获，不阻断其他插件。"""
        ...

    def teardown(self) -> None:
        """卸载/退出时释放资源（可选实现）。"""
        ...
