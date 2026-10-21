"""Java 运行时探测与选择。"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "JavaCandidate",
    "JavaNotFound",
    "detect_java",
    "list_java",
    "probe_java",
]

_CACHE: dict[str, tuple[float, int | None]] = {}
_VERSION_RE = re.compile(r'version "([^"]+)"')


@dataclass(frozen=True, slots=True)
class JavaCandidate:
    path: Path
    major: int
    label: str
    source: str

    @property
    def display(self) -> str:
        return f"Java {self.major}（{self.source}）"


class JavaNotFound(RuntimeError):
    def __init__(self, required: int | None, found: list[JavaCandidate]) -> None:
        self.required = required
        self.found = found
        detail = "，".join(f"{c.path} (Java {c.major})" for c in found[:8]) or "未发现任何 Java"
        need = f"需要 Java {required}" if required else "需要 Java 8 或更高版本"
        super().__init__(f"{need}，但{detail}")
        self.detail = detail


def _major_from_version(text: str) -> int | None:
    text = text.strip()
    if text.startswith("1."):
        text = text[2:]
    match = re.match(r"(\d+)", text)
    return int(match.group(1)) if match else None


def _major_from_release(release: Path) -> int | None:
    try:
        for line in release.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("JAVA_VERSION="):
                return _major_from_version(line.split("=", 1)[1].strip('"'))
    except OSError:
        return None
    return None


def probe_java(java_exe: Path | str) -> int | None:
    """读取 Java 主版本；优先 release 文件，必要时执行 ``java -version``。"""
    path = Path(java_exe)
    if not path.is_file():
        return None
    key = str(path.resolve()).lower()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    cached = _CACHE.get(key)
    if cached and cached[0] == mtime:
        return cached[1]

    major = _major_from_release(path.parent.parent / "release")
    if major is None:
        flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
        try:
            proc = subprocess.run(
                [str(path), "-version"],
                capture_output=True,
                text=True,
                timeout=15,
                creationflags=flags,
            )
            output = (proc.stderr or "") + (proc.stdout or "")
            match = _VERSION_RE.search(output)
            if match:
                major = _major_from_version(match.group(1))
        except (OSError, subprocess.SubprocessError):
            major = None
    _CACHE[key] = (mtime, major)
    return major


def _add(candidates: list[JavaCandidate], path: Path | None, source: str) -> None:
    if path is None:
        return
    path = Path(path)
    if not path.is_file():
        return
    resolved = path.resolve()
    if any(item.path.resolve() == resolved for item in candidates):
        return
    major = probe_java(path)
    if major is None:
        return
    candidates.append(JavaCandidate(path=path, major=major, label=f"Java {major}", source=source))


def list_java(mc_dir: Path | str) -> list[JavaCandidate]:
    """枚举可用 Java：本地 runtime 优先，然后是 JAVA_HOME、常见安装目录与 PATH。"""
    mc_dir = Path(mc_dir)
    candidates: list[JavaCandidate] = []

    runtime_dir = mc_dir / "runtime"
    if runtime_dir.is_dir():
        for component in sorted(runtime_dir.iterdir()):
            if component.is_dir():
                _add(candidates, component / "bin" / "java.exe", f"runtime/{component.name}")

    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        _add(candidates, Path(java_home) / "bin" / "java.exe", "JAVA_HOME")

    patterns = [
        (Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Java", "Program Files/Java"),
        (
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Eclipse Adoptium",
            "Eclipse Adoptium",
        ),
        (
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Microsoft",
            "Microsoft JDK",
        ),
        (
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Eclipse Adoptium",
            "Adoptium (user)",
        ),
        (
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Microsoft",
            "Microsoft JDK (user)",
        ),
        (
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "JetBrains",
            "JetBrains JBR",
        ),
    ]
    for base, label in patterns:
        if not base or not base.is_dir():
            continue
        for entry in sorted(base.glob("*")):
            exe = entry / "bin" / "java.exe"
            if not exe.is_file():
                exe = entry / "jbr" / "bin" / "java.exe"
            _add(candidates, exe, label)

    which = shutil.which("java")
    if which:
        _add(candidates, Path(which), "PATH")

    return candidates


def detect_java(
    mc_dir: Path | str,
    required: int | None,
    *,
    override: str | Path | None = None,
    preferred_component: str | None = None,
) -> JavaCandidate:
    """选择 Java：精确主版本优先，其次更高版本；``override`` 强制使用。"""
    if override:
        path = Path(override)
        major = probe_java(path)
        if major is None:
            raise JavaNotFound(required, [])
        candidate = JavaCandidate(path=path, major=major, label=f"Java {major}", source="手动指定")
        if required and major < required:
            raise JavaNotFound(required, [candidate])
        return candidate

    candidates = list_java(mc_dir)
    if not candidates:
        raise JavaNotFound(required, [])

    if preferred_component:
        for candidate in candidates:
            if preferred_component in candidate.source:
                if required is None or candidate.major >= required:
                    return candidate

    if required is None:
        for candidate in candidates:
            if candidate.major == 8:
                return candidate
        return sorted(candidates, key=lambda c: c.major)[0]

    exact = [c for c in candidates if c.major == required]
    if exact:
        return exact[0]
    higher = sorted((c for c in candidates if c.major > required), key=lambda c: c.major)
    if higher:
        return higher[0]
    raise JavaNotFound(required, candidates)
