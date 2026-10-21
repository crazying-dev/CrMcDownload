"""离线账户：用户名校验与 v3 UUID。"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass

__all__ = ["OfflineProfile", "NAME_PATTERN", "make_profile", "offline_uuid"]

NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{3,16}$")


@dataclass(frozen=True, slots=True)
class OfflineProfile:
    name: str
    uuid: str
    access_token: str = "0"
    user_type: str = "legacy"
    xuid: str = ""

    @property
    def auth_session(self) -> str:
        """1.6 之前的 ``${auth_session}`` 格式。"""
        return f"token:{self.access_token}:{self.uuid}"


def validate_name(name: str) -> str:
    name = (name or "").strip()
    if not NAME_PATTERN.match(name):
        raise ValueError("用户名需为 3-16 位字母、数字或下划线")
    return name


def offline_uuid(name: str) -> str:
    """Mojang 离线 UUID：``MD5("OfflinePlayer:" + name)`` 规范化为 v3。"""
    digest = bytearray(hashlib.md5(("OfflinePlayer:" + name).encode("utf-8")).digest())
    digest[6] = (digest[6] & 0x0F) | 0x30
    digest[8] = (digest[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(digest)))


def make_profile(name: str) -> OfflineProfile:
    name = validate_name(name)
    return OfflineProfile(name=name, uuid=offline_uuid(name))
