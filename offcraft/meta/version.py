"""版本 JSON：读取、``inheritsFrom`` 合并与字段访问。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "MergedVersion",
    "VersionError",
    "load_version_file",
    "load_version",
    "merge_versions",
    "resolve_chain",
]

_MAX_DEPTH = 8


class VersionError(RuntimeError):
    """版本 JSON 缺失、损坏或继承关系非法。"""


@dataclass(slots=True)
class MergedVersion:
    id: str
    raw: dict[str, Any]
    chain: tuple[str, ...] = ()

    # -- 基础字段 -------------------------------------------------------
    @property
    def type(self) -> str:
        return self.raw.get("type", "release")

    @property
    def main_class(self) -> str:
        value = self.raw.get("mainClass")
        if not value:
            raise VersionError(f"版本 {self.id} 缺少 mainClass")
        return value

    @property
    def libraries(self) -> list[dict[str, Any]]:
        return list(self.raw.get("libraries") or [])

    @property
    def arguments(self) -> dict[str, list[Any]]:
        return self.raw.get("arguments") or {}

    @property
    def minecraft_arguments(self) -> str | None:
        return self.raw.get("minecraftArguments")

    @property
    def assets(self) -> str:
        return self.raw.get("assets") or "legacy"

    @property
    def asset_index(self) -> dict[str, Any] | None:
        return self.raw.get("assetIndex")

    @property
    def downloads(self) -> dict[str, Any]:
        return self.raw.get("downloads") or {}

    @property
    def java_major(self) -> int | None:
        value = (self.raw.get("javaVersion") or {}).get("majorVersion")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @property
    def jar_id(self) -> str:
        return self.raw.get("jar") or self.id

    @property
    def logging_client(self) -> dict[str, Any] | None:
        return (self.raw.get("logging") or {}).get("client")

    def merged_libraries(self) -> list[dict[str, Any]]:
        return self.libraries


def merge_versions(parent: dict[str, Any], child: dict[str, Any]) -> dict[str, Any]:
    """child 覆盖 parent；libraries 与 arguments 追加。"""
    merged = dict(parent)
    for key, value in child.items():
        if key in ("libraries",):
            continue
        if key == "arguments":
            parent_args = parent.get("arguments") or {}
            merged["arguments"] = {
                "game": list(parent_args.get("game") or []) + list(value.get("game") or []),
                "jvm": list(parent_args.get("jvm") or []) + list(value.get("jvm") or []),
            }
        else:
            merged[key] = value
    merged["libraries"] = list(parent.get("libraries") or []) + list(child.get("libraries") or [])
    return merged


def load_version_file(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError as exc:
        raise VersionError(f"找不到版本文件：{path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise VersionError(f"版本文件无法解析：{path}（{exc}）") from exc
    if not isinstance(data, dict):
        raise VersionError(f"版本文件格式错误：{path}")
    return data


def _read_raw(mc_dir: Path, version_id: str) -> dict[str, Any]:
    path = mc_dir / "versions" / version_id / f"{version_id}.json"
    if not path.is_file():
        raise VersionError(f"版本 {version_id} 未安装（缺少 {path}）")
    return load_version_file(path)


def resolve_chain(mc_dir: Path, version_id: str) -> list[dict[str, Any]]:
    """返回从祖先到自身的版本 JSON 列表，检测环与超深继承。"""
    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    current: str | None = version_id
    while current:
        if current in seen:
            raise VersionError(f"版本继承出现环：{' -> '.join([c['id'] for c in chain] + [current])}")
        seen.add(current)
        raw = _read_raw(mc_dir, current)
        raw.setdefault("id", current)
        chain.append(raw)
        if len(chain) > _MAX_DEPTH:
            raise VersionError(f"版本继承层级超过 {_MAX_DEPTH}：{version_id}")
        current = raw.get("inheritsFrom")
    chain.reverse()
    return chain


def load_version(mc_dir: Path | str, version_id: str) -> MergedVersion:
    """读取并合并版本（含 inheritsFrom）。"""
    mc_dir = Path(mc_dir)
    chain = resolve_chain(mc_dir, version_id)
    merged = chain[0]
    for layer in chain[1:]:
        merged = merge_versions(merged, layer)
    merged["id"] = version_id
    return MergedVersion(id=version_id, raw=merged, chain=tuple(item["id"] for item in chain))
