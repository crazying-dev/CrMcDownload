"""账户提供者抽象：离线账户的现包装，外置登录的接入位。

本次只交付抽象 + 离线实现 + 启动参数注入位（``-javaagent``、``accessToken``、
``user_type``）。后续接 LittleSkin / authlib-injector 只需新增一个
:class:`AccountProvider` 实现并注册到 :func:`get_provider`，界面与启动主链路不改。

:class:`LaunchAccount` 与旧的 :class:`offcraft.launch.auth.OfflineProfile` 字段兼容
（``name/uuid/access_token/user_type/xuid/auth_session``），command.py 可直接消费。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from offcraft.launch.auth import OfflineProfile, offline_uuid, validate_name

__all__ = [
    "AccountProvider",
    "LaunchAccount",
    "OfflineAccount",
    "get_provider",
    "register_provider",
]


@dataclass(frozen=True, slots=True)
class LaunchAccount:
    """一次启动所需的账户信息（离线 / 外置登录统一形态）。"""

    name: str
    uuid: str
    access_token: str = "0"
    user_type: str = "legacy"
    xuid: str = ""
    java_agent: str | None = None
    extra_jvm: list[str] = field(default_factory=list)

    @property
    def auth_session(self) -> str:
        return f"token:{self.access_token}:{self.uuid}"

    def to_profile(self) -> OfflineProfile:
        """向后兼容：转成旧的 :class:`OfflineProfile`（丢弃 agent/jvm）。"""
        return OfflineProfile(
            name=self.name,
            uuid=self.uuid,
            access_token=self.access_token,
            user_type=self.user_type,
            xuid=self.xuid,
        )


@runtime_checkable
class AccountProvider(Protocol):
    """账户提供者接口。"""

    id: str
    label: str

    def acquire(self, config: Any) -> LaunchAccount:
        """从配置里取出/生成本次启动要用的账户。"""
        ...

    def capabilities(self) -> set[str]:
        """能力声明：``skin`` / ``cape`` / ``online-mode-server`` 等。"""
        ...


class OfflineAccount:
    """默认实现：把现有 ``make_profile`` 包一层，账户不可在线校验。"""

    id = "offline"
    label = "离线账户"

    def acquire(self, config: Any) -> LaunchAccount:
        name = validate_name(getattr(config, "username", "") or "Player")
        return LaunchAccount(name=name, uuid=offline_uuid(name))

    def capabilities(self) -> set[str]:
        # 离线账户没有官方 GameProfile：皮肤只能靠 CSL mod
        return {"skin"}


_PROVIDERS: dict[str, AccountProvider] = {"offline": OfflineAccount()}


def register_provider(provider: AccountProvider) -> None:
    """注册一个账户提供者（外置登录实现在这里挂进来）。"""
    _PROVIDERS[provider.id] = provider


def get_provider(config: Any) -> AccountProvider:
    """按 ``config.account_provider`` 取提供者，缺失时回落离线。"""
    pid = str(getattr(config, "account_provider", "offline") or "offline")
    return _PROVIDERS.get(pid) or _PROVIDERS["offline"]
