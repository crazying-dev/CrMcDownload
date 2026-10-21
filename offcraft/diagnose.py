"""启动前检查：把失败原因说清楚。"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from offcraft.launch.auth import validate_name
from offcraft.launch.java import JavaNotFound, detect_java
from offcraft.launch.libraries import build_classpath
from offcraft.meta.version import VersionError, load_version

__all__ = ["Check", "preflight"]


@dataclass(frozen=True, slots=True)
class Check:
    level: str  # ok / warn / error
    title: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.level != "error"


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path, prefix=".offcraft-", delete=True):
            return True
    except OSError:
        return False


def preflight(
    mc_dir: Path | str,
    version_id: str,
    *,
    username: str = "Player",
    memory_mb: int = 4096,
) -> list[Check]:
    mc_dir = Path(mc_dir)
    checks: list[Check] = []

    try:
        version = load_version(mc_dir, version_id)
    except VersionError as exc:
        return [Check("error", "版本不可用", str(exc))]
    checks.append(Check("ok", "版本 JSON", f"{version.id}（{version.type}）"))

    jar_id = version.jar_id
    jar = mc_dir / "versions" / jar_id / f"{jar_id}.jar"
    checks.append(
        Check("ok", "客户端 jar", str(jar))
        if jar.is_file()
        else Check("error", "客户端 jar 缺失", str(jar))
    )

    classpath = build_classpath(version, mc_dir)
    if classpath.missing:
        preview = "、".join(path.name for path in classpath.missing[:6])
        checks.append(
            Check("error", "依赖不完整", f"缺少 {len(classpath.missing)} 个文件：{preview}")
        )
    else:
        checks.append(Check("ok", "依赖库", f"{len(classpath.entries)} 个 classpath 条目"))

    index_id = version.assets
    index_path = mc_dir / "assets" / "indexes" / f"{index_id}.json"
    if index_path.is_file():
        checks.append(Check("ok", "资源索引", index_id))
    else:
        checks.append(Check("warn", "资源索引缺失", f"{index_path}（游戏可启动，但无声音/语言）"))

    component = (version.raw.get("javaVersion") or {}).get("component")
    try:
        java = detect_java(mc_dir, version.java_major, preferred_component=component)
        checks.append(Check("ok", "Java", java.display))
    except JavaNotFound as exc:
        checks.append(Check("error", "Java 不可用", str(exc)))

    if _writable(mc_dir):
        checks.append(Check("ok", "游戏目录可写", str(mc_dir)))
    else:
        checks.append(Check("error", "游戏目录不可写", str(mc_dir)))

    try:
        validate_name(username)
        checks.append(Check("ok", "用户名", username))
    except ValueError as exc:
        checks.append(Check("error", "用户名非法", str(exc)))

    if memory_mb < 1024:
        checks.append(Check("warn", "内存偏小", f"{memory_mb} MB 可能不足以启动"))
    checks.append(Check("ok", "内存", f"{memory_mb} MB"))
    return checks


def newest_crash_report(mc_dir: Path | str) -> Path | None:  # pragma: no cover - UI 辅助
    folder = Path(mc_dir) / "crash-reports"
    if not folder.is_dir():
        return None
    reports = sorted(folder.glob("crash-*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    return reports[0] if reports else None


def _noop() -> None:
    os.environ.get("PATH")
