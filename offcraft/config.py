"""配置读写：JSON、原子写、损坏自愈。"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["Config", "config_dir", "load_config", "save_config"]

SCHEMA = 1


def config_dir(explicit: str | Path | None = None) -> Path:
    """配置目录：显式参数 > OFFCRAFT_DATA > %APPDATA%/OffCraft。"""
    if explicit:
        return Path(explicit)
    env = os.environ.get("OFFCRAFT_DATA")
    if env:
        return Path(env)
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "OffCraft"
    return Path.home() / ".offcraft"


@dataclass
class Config:
    schema: int = SCHEMA
    download_base: str = "https://mc.yjlt.top"
    download_token: str = ""
    #: 允许下载整合包里指向未知主机的地址（默认关闭，只放行白名单域）
    allow_untrusted_sources: bool = False
    #: 每次下载前比较镜像与直连的时延，择快者使用
    prefer_fastest: bool = True
    proxy: str = ""
    max_workers: int = 8
    skip_assets: bool = False
    #: 版本隔离：每个版本使用 ``versions/<id>`` 作为游戏目录（mods/存档/配置互不影响）
    isolation: bool = True
    username: str = "Player"
    memory_mb: int = 4096
    width: int = 854
    height: int = 480
    fullscreen: bool = False
    game_dir: str = ""
    java_overrides: dict[str, str] = field(default_factory=dict)
    version_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    extra_jvm: list[str] = field(default_factory=list)
    last_version: str = ""
    recent_dirs: list[str] = field(default_factory=list)
    window: dict[str, int] = field(default_factory=lambda: {"w": 1180, "h": 760})
    java_cache: dict[str, Any] = field(default_factory=dict)
    #: 联机：是否允许探听内网/回环服务器（默认关，避免把客户端变成 SSRF 跳板）
    allow_local_servers: bool = False
    #: 皮肤：是否启用 CSL 自动安装与上传入口
    skin_enabled: bool = True
    #: 皮肤模型：``auto`` 按图片判定 / ``classic`` / ``slim``
    skin_model: str = "auto"
    #: 账户提供者（本次仅 ``offline``；预留给外置登录）
    account_provider: str = "offline"
    #: 离线联机（P2P）：默认关闭，需显式开启（特权插件，走端到端加密隧道）
    p2p_enabled: bool = False
    #: 中继端口：0 = 由服务端下发（信令响应里携带）；非 0 仅作本地覆盖预留
    p2p_relay_port: int = 0
    #: 上次使用的房间名（仅标识；密码改由机器绑定加密存于插件存储）
    p2p_last_room: str = ""
    #: 稳定设备标识（首启生成）：服务端每设备房间配额的软锚点；重装/清配置会重置
    p2p_device_id: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Config":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        clean = {key: value for key, value in data.items() if key in known}
        config = cls(**clean)
        config.normalize()
        return config

    def normalize(self) -> None:
        self.memory_mb = max(512, int(self.memory_mb))
        self.max_workers = max(1, min(32, int(self.max_workers)))
        self.width = max(320, int(self.width))
        self.height = max(240, int(self.height))
        self.download_base = (self.download_base or "").strip().rstrip("/")
        if self.skin_model not in ("auto", "classic", "slim"):
            self.skin_model = "auto"
        if not self.account_provider:
            self.account_provider = "offline"
        if not self.username:
            self.username = "Player"
        self.p2p_relay_port = max(0, min(65535, int(self.p2p_relay_port)))
        self.p2p_last_room = (self.p2p_last_room or "").strip()[:64]
        # 设备标识：缺失/非法则生成一个稳定的 32 位 hex（首启落盘后不再变）
        if not (8 <= len(self.p2p_device_id or "") <= 64
                and all(c in "0123456789abcdef" for c in (self.p2p_device_id or ""))):
            import secrets
            self.p2p_device_id = secrets.token_hex(16)

    def apply_version_overrides(self, version_id: str) -> dict[str, Any]:
        data = asdict(self)
        data.update(self.version_overrides.get(version_id) or {})
        return data


def load_config(data_dir: str | Path | None = None) -> Config:
    directory = config_dir(data_dir)
    path = directory / "config.json"
    if not path.is_file():
        return Config()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("配置根节点不是对象")
        return Config.from_json(data)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        backup = directory / f"config.corrupt-{time.strftime('%Y%m%d-%H%M%S')}.json"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup)
        except OSError:
            pass
        return Config()


def save_config(config: Config, data_dir: str | Path | None = None) -> Path:
    directory = config_dir(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "config.json"
    tmp = path.with_name("config.json.tmp")
    config.normalize()
    tmp.write_text(json.dumps(config.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path
