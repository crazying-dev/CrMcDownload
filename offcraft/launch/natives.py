"""natives 解压：跳过 META-INF、排除项与路径穿越。"""

from __future__ import annotations

import zipfile
from pathlib import Path

__all__ = ["extract_natives"]


def extract_natives(
    jars: list[Path],
    dest: Path | str,
    excludes: list[str] | tuple[str, ...] = (),
) -> int:
    """把 natives jar 解压到 ``dest``，返回写入文件数。"""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    written = 0
    for jar in jars:
        if not Path(jar).is_file():
            continue
        with zipfile.ZipFile(jar) as zf:
            for info in zf.infolist():
                name = info.filename
                if info.is_dir():
                    continue
                if name.startswith("META-INF/"):
                    continue
                if any(name.startswith(prefix) for prefix in excludes):
                    continue
                safe = Path(name)
                if safe.is_absolute() or ".." in safe.parts or ":" in name:
                    continue
                target = dest / safe
                if target.is_file() and target.stat().st_size == info.file_size:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, target.open("wb") as out:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                written += 1
    return written


def natives_directory(mc_dir: Path | str, version_id: str) -> Path:
    return Path(mc_dir) / "versions" / version_id / f"{version_id}-natives"


def ensure_natives(
    version_id: str,
    mc_dir: Path | str,
    native_jars: list[Path],
    excludes: list[str] | tuple[str, ...] = (),
) -> Path:
    target = natives_directory(mc_dir, version_id)
    target.mkdir(parents=True, exist_ok=True)
    if native_jars:
        extract_natives(native_jars, target, excludes)
    return target


def _noop() -> None:  # pragma: no cover - 保留给类型检查
    os.environ.get("PATH")
