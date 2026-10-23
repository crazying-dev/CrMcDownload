"""磁盘缓存：路径映射、TTL 与原子写。"""

from __future__ import annotations

import os
import time
from pathlib import Path

__all__ = ["Cache"]

#: 元数据类路由使用 TTL；其余（哈希命名）视为不可变
_METADATA_PREFIXES = ("meta", "fabric", "modrinth")


class Cache:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, key: str) -> Path:
        """key 形如 ``meta/mc/game/version_manifest_v2.json``。"""
        target = (self.root / key).resolve()
        if self.root != target and self.root not in target.parents:
            raise ValueError(f"缓存路径越界：{key}")
        return target

    def is_fresh(self, path: Path, ttl: int, immutable: bool = False) -> bool:
        try:
            stat = path.stat()
        except OSError:
            return False
        if not path.is_file():
            return False
        if immutable:
            return True
        return (time.time() - stat.st_mtime) < ttl

    def begin_write(self, path: Path) -> tuple[Path, object]:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        return tmp, tmp.open("wb")

    def commit(self, tmp: Path, path: Path) -> None:
        os.replace(tmp, path)

    def total_bytes(self) -> int:
        total = 0
        for root, _dirs, files in os.walk(self.root):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
        return total


def is_metadata(prefix: str, relative: str = "") -> bool:
    """元数据类资源（短 TTL）：元数据路由，或 maven 的 ``*.xml`` 索引。"""
    if prefix in _METADATA_PREFIXES:
        return True
    name = str(relative or "").rsplit("/", 1)[-1].lower()
    return name.endswith(".xml")
