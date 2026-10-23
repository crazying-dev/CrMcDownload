"""磁盘缓存：路径映射、TTL、容量上限、过期回收与原子写。

容量策略（对应 Issue #1 P1“缓存容量限制未生效”）：

* 单文件超限不进缓存，但仍会原样转发给客户端；
* 总量超限时按 mtime 从旧到新淘汰（LRU）；
* 过期元数据、残留临时文件由 :meth:`Cache.sweep_expired` 定期清掉；
* 磁盘写满时 :meth:`Cache.free_space` 清临时文件并压缩到容量一半。

临时文件名带 ``pid + 线程号 + 随机后缀``，同一进程内多个线程写
同一个键也不会互相覆盖（Issue #1 P2）。
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from pathlib import Path

__all__ = ["Cache"]

#: 元数据类路由使用 TTL；其余（哈希命名）视为不可变
_METADATA_PREFIXES = ("meta", "fabric", "modrinth")

_TEMP_SUFFIX = ".tmp"


def is_metadata(prefix: str, relative: str = "") -> bool:
    """元数据类资源（短 TTL）：元数据路由，或 maven 的 ``*.xml`` 索引。"""
    if prefix in _METADATA_PREFIXES:
        return True
    name = str(relative or "").rsplit("/", 1)[-1].lower()
    return name.endswith(".xml")


class Cache:
    """磁盘缓存。

    :param max_bytes: 缓存总量上限，0 表示不限
    :param max_file_bytes: 单文件入缓存上限，0 表示不限
    """

    def __init__(
        self,
        root: Path | str,
        *,
        max_bytes: int = 0,
        max_file_bytes: int = 0,
    ) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max(0, int(max_bytes or 0))
        self.max_file_bytes = max(0, int(max_file_bytes or 0))
        self.evicted_bytes = 0
        self.swept_bytes = 0
        self._lock = threading.RLock()
        #: 序列化 os.replace：Windows 上并发替换同名文件会报「拒绝访问」
        self._commit_lock = threading.Lock()
        self._total: int | None = None
        self._writing: set[Path] = set()
        self._last_sweep = 0.0

    # ------------------------------------------------------------- 路径
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

    # ------------------------------------------------------------- 容量
    def total_bytes(self, *, refresh: bool = False) -> int:
        """已用缓存字节数（首次调用扫盘，之后靠增量维护）。"""
        with self._lock:
            if self._total is None or refresh:
                self._total = sum(size for _m, size, _p in self._entries())
            return self._total

    def allows(self, size: int) -> bool:
        """单个文件能否进缓存（超单文件上限或超整体上限时返回 False）。"""
        size = int(size or 0)
        if size <= 0:
            return True
        if self.max_file_bytes and size > self.max_file_bytes:
            return False
        if self.max_bytes and size > self.max_bytes:
            return False
        return True

    def _entries(self, *, include_temp: bool = False) -> list[tuple[float, int, Path]]:
        """缓存文件列表 ``[(mtime, size, path)]``；默认不算临时文件。"""
        with self._lock:
            writing = set(self._writing)
        items: list[tuple[float, int, Path]] = []
        for current, _dirs, files in os.walk(self.root):
            for name in files:
                path = Path(current) / name
                if path in writing:
                    continue
                if not include_temp and name.endswith(_TEMP_SUFFIX):
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                items.append((stat.st_mtime, stat.st_size, path))
        return items

    def evict(
        self,
        *,
        keep: Path | None = None,
        needed: int = 0,
        target: int | None = None,
    ) -> int:
        """按 mtime 从旧到新删除，直到总量不超上限；返回释放的字节数。"""
        limit = self.max_bytes if target is None else max(0, int(target))
        if not limit:
            return 0
        entries = self._entries()
        total = sum(size for _m, size, _p in entries)
        if total <= limit and needed <= 0:
            with self._lock:
                self._total = total
            return 0
        entries.sort(key=lambda item: (item[0], str(item[2])))
        removed = 0
        for _mtime, size, path in entries:
            if total <= limit and removed >= needed:
                break
            if keep is not None and path == keep:
                continue
            try:
                path.unlink()
            except OSError:
                continue
            total -= size
            removed += size
        with self._lock:
            self._total = max(total, 0)
        self.evicted_bytes += removed
        return removed

    def purge_temp(self, *, max_age: float = 3600.0) -> int:
        """删除残留临时文件（``max_age=0`` 表示全部删除，正在写的除外）。"""
        now = time.time()
        removed = 0
        for mtime, size, path in self._entries(include_temp=True):
            if not path.name.endswith(_TEMP_SUFFIX):
                continue
            if max_age and (now - mtime) < max_age:
                continue
            try:
                path.unlink()
            except OSError:
                continue
            removed += size
        return removed

    def sweep_expired(
        self,
        ttl: int,
        *,
        min_interval: float = 300.0,
        now: float | None = None,
    ) -> int:
        """删除过期元数据与长期残留的临时文件；返回释放的字节数。

        ``min_interval`` 内的重复调用直接返回 0，避免每个请求都扫盘。
        """
        moment = time.time() if now is None else float(now)
        with self._lock:
            if min_interval and (moment - self._last_sweep) < min_interval:
                return 0
            self._last_sweep = moment
        removed = 0
        for mtime, size, path in self._entries(include_temp=True):
            age = moment - mtime
            if path.name.endswith(_TEMP_SUFFIX):
                if age <= max(ttl, 3600):
                    continue
            else:
                try:
                    relative = path.relative_to(self.root).as_posix()
                except ValueError:
                    continue
                prefix = relative.split("/", 1)[0]
                if not is_metadata(prefix, relative) or age <= ttl:
                    continue
            try:
                path.unlink()
            except OSError:
                continue
            removed += size
        if removed:
            with self._lock:
                if self._total is not None:
                    self._total = max(0, self._total - removed)
        self.swept_bytes += removed
        return removed

    def free_space(self) -> int:
        """磁盘紧张时自救：先清残留临时文件，再把用量压到上限的一半。"""
        removed = self.purge_temp(max_age=0)
        if self.max_bytes:
            removed += self.evict(target=self.max_bytes // 2)
        return removed

    def remove(self, path: Path) -> None:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        try:
            path.unlink()
        except OSError:
            return
        with self._lock:
            if self._total is not None:
                self._total = max(0, self._total - size)

    def stats(self) -> dict[str, int]:
        """给 /health 用的容量快照。"""
        total = self.total_bytes()
        return {
            "bytes": total,
            "max_bytes": self.max_bytes,
            "max_file_bytes": self.max_file_bytes,
            "evicted_bytes": self.evicted_bytes,
            "swept_bytes": self.swept_bytes,
        }

    # ------------------------------------------------------------- 写入
    def begin_write(self, path: Path) -> tuple[Path, object]:
        """开始写临时文件；名字唯一，同进程多线程互不干扰。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(
            f"{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}{_TEMP_SUFFIX}"
        )
        with self._lock:
            self._writing.add(tmp)
        return tmp, tmp.open("wb")

    def abort_write(self, tmp: Path) -> None:
        """放弃本次写入：删临时文件并取消登记。"""
        with self._lock:
            self._writing.discard(tmp)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass

    def commit(self, tmp: Path, path: Path, *, size: int | None = None) -> None:
        """原子替换目标文件，必要时淘汰旧条目。"""
        if size is None:
            try:
                size = tmp.stat().st_size
            except OSError:
                size = 0
        with self._lock:
            self._writing.discard(tmp)
        self._replace(tmp, path)
        with self._lock:
            if self._total is not None:
                self._total += int(size)
        if self.max_bytes:
            self.evict(keep=path)

    def _replace(self, tmp: Path, path: Path) -> None:
        """把临时文件换到目标位置；并发/占用时短暂重试。"""
        with self._commit_lock:
            last: OSError | None = None
            for attempt in range(6):
                try:
                    os.replace(tmp, path)
                    return
                except OSError as exc:
                    last = exc
                    time.sleep(min(0.02 * (attempt + 1), 0.2))
            # 目标可能被读线程占用：先摘掉再换，最后再试一次
            try:
                path.unlink(missing_ok=True)
                os.replace(tmp, path)
                return
            except OSError:
                self.abort_write(tmp)
                raise last  # type: ignore[misc]

