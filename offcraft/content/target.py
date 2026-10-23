"""内容安装目标：已装版本（实例）还是任意文件夹。

模组 / 资源包 / 光影必须写进游戏目录下的固定子目录，所以「能不能装」按目标目录里
是否已有对应子目录判断；版本目录与 ``.minecraft`` 根目录缺子目录时安装会自动创建。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from offcraft.content.loader import supports_mods
from offcraft.instances import Instance, detect_loader, instance_game_dir

__all__ = [
    "CONTENT_SUBDIRS",
    "DIR_OTHER",
    "DIR_ROOT",
    "DIR_VERSION",
    "KIND_NAMES",
    "Target",
    "target_from_folder",
    "target_from_instance",
    "target_state",
]

#: 工程类型 → 游戏目录下的子目录
CONTENT_SUBDIRS = {
    "mod": "mods",
    "resourcepack": "resourcepacks",
    "shader": "shaderpacks",
}

KIND_NAMES = {
    "mod": "模组",
    "resourcepack": "资源包",
    "shader": "光影",
}

#: 目标目录的形态
DIR_VERSION = "version"
DIR_ROOT = "root"
DIR_OTHER = "other"

_VERSION_RE = re.compile(r"^\d+(?:\.\d+)+")


@dataclass(frozen=True, slots=True)
class Target:
    """内容安装位置（版本实例或手选文件夹）。"""

    path: Path
    label: str
    source: str = "instance"
    instance_id: str = ""
    mc_version: str = ""
    loader: str = ""
    dir_kind: str = DIR_OTHER

    @property
    def key(self) -> str:
        return str(self.path).lower()

    @property
    def is_game_dir(self) -> bool:
        """是版本目录或 .minecraft 根目录（缺子目录时允许自动创建）。"""
        return self.dir_kind in (DIR_VERSION, DIR_ROOT)

    def subdir(self, kind: str) -> Path:
        return Path(self.path) / CONTENT_SUBDIRS[kind]


def _read_version(folder: Path) -> tuple[str, str]:
    """读 ``<folder>/<folder>.json``，返回 ``(mc_version, loader)``。"""
    path = folder / f"{folder.name}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", ""
    if not isinstance(raw, dict):
        return "", ""
    mc = str(raw.get("clientVersion") or raw.get("inheritsFrom") or "")
    if not mc:
        match = _VERSION_RE.match(folder.name)
        mc = match.group(0) if match else ""
    libraries = raw.get("libraries")
    loader = detect_loader(str(raw.get("mainClass") or ""), list(libraries or []))
    return mc, loader


def _infer(folder: Path) -> tuple[str, str]:
    """从文件夹（版本目录或 .minecraft 根）推断 MC 版本与加载器。"""
    folder = Path(folder)
    versions = folder / "versions"
    if versions.is_dir():
        candidates = [
            item
            for item in versions.iterdir()
            if item.is_dir() and (item / f"{item.name}.json").is_file()
        ]
        candidates.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        for candidate in candidates:
            mc, loader = _read_version(candidate)
            if mc or loader:
                return mc, loader
        return "", ""
    return _read_version(folder)


def _dir_kind(folder: Path) -> str:
    if (folder / "versions").is_dir():
        return DIR_ROOT
    if (folder / f"{folder.name}.json").is_file():
        return DIR_VERSION
    return DIR_OTHER


def target_from_instance(
    mc_dir: Path | str, instance: Instance, *, isolated: bool = True
) -> Target:
    """已装版本 → 目标（游戏目录由版本隔离决定）。"""
    path = instance_game_dir(mc_dir, instance.id, isolated)
    return Target(
        path=Path(path),
        label=f"{instance.id}（{instance.loader}）",
        source="instance",
        instance_id=instance.id,
        mc_version=instance.mc_version,
        loader=instance.loader,
        dir_kind=DIR_VERSION if isolated else DIR_ROOT,
    )


def target_from_folder(folder: Path | str) -> Target:
    """手选文件夹 → 目标（自动读版本 JSON / 判断是否 .minecraft 根）。"""
    path = Path(folder).expanduser()
    mc_version, loader = _infer(path)
    label = f"{path.name or path}（文件夹）"
    return Target(
        path=path,
        label=label,
        source="folder",
        mc_version=mc_version,
        loader=loader,
        dir_kind=_dir_kind(path) if path.is_dir() else DIR_OTHER,
    )


def target_state(target: Target, kind: str) -> tuple[bool, str]:
    """目标能否安这种内容，返回 ``(可以, 提示)``。"""
    sub = CONTENT_SUBDIRS.get(kind)
    if sub is None:
        return True, ""
    name = KIND_NAMES.get(kind, kind)
    folder = Path(target.path)
    has_dir = (folder / sub).is_dir()
    blocked = bool(target.loader) and not supports_mods(target.loader)
    if has_dir:
        if blocked and kind in ("mod", "shader"):
            return False, f"{target.label} 是原版实例，不能装{name}（先装加载器）"
        return True, ""
    if blocked and kind in ("mod", "shader"):
        return False, f"{target.label} 是原版实例，不能装{name}（先装加载器）"
    if target.is_game_dir:
        return True, f"目标下还没有 {sub}/，安装时自动创建"
    return False, f"{folder.name or folder} 下没有 {sub}/，不是可安装{name}的目录"
