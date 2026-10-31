"""natives 解压：跳过 META-INF、排除项与路径穿越；用清单文件整目录跳过重复解压。"""

from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

__all__ = ["ensure_natives", "extract_natives", "natives_directory"]

#: 清单文件名：记录 native jar 指纹与排除项，命中即整目录跳过扫描/解压
_MANIFEST = ".done.json"


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


def _fingerprint(jars: list[Path], excludes: list[str] | tuple[str, ...]) -> dict:
    """native jar 的 ``{路径: [mtime_ns, size]}`` 指纹 + 排除项，用于判定是否需重解压。"""
    files: dict[str, list[int]] = {}
    for jar in jars:
        p = Path(jar)
        try:
            st = p.stat()
        except OSError:
            # 文件此刻不可读：用哨兵值，命中检查必然失败，触发一次真实解压
            files[str(p)] = [-1, -1]
            continue
        files[str(p)] = [st.st_mtime_ns, st.st_size]
    return {"files": files, "excludes": list(excludes)}


def _manifest_path(target: Path) -> Path:
    return target / _MANIFEST


def _manifest_matches(target: Path, fp: dict) -> bool:
    """读清单，与当前指纹逐项比对；不匹配即重新解压。

    ``_fingerprint`` 已对每个 jar 调过 ``stat``：任一 jar 缺失都会记成哨兵
    ``[-1, -1]``，与清单里的历史值必然不等，因此无需再单独确认磁盘存在。
    """
    try:
        saved = json.loads(_manifest_path(target).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(saved, dict):
        return False
    return saved.get("excludes") == fp["excludes"] and (saved.get("files") or {}) == fp["files"]


def _write_manifest(target: Path, fp: dict) -> None:
    manifest = _manifest_path(target)
    tmp = manifest.with_name(_MANIFEST + ".importing")
    try:
        tmp.write_text(json.dumps(fp, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, manifest)
    except OSError:
        # 清单写失败只是下次重新解压，不影响本次启动
        try:
            tmp.unlink()
        except OSError:
            pass


def ensure_natives(
    version_id: str,
    mc_dir: Path | str,
    native_jars: list[Path],
    excludes: list[str] | tuple[str, ...] = (),
) -> Path:
    """解压 natives；命中清单（jar 指纹 + 排除项未变）则整目录跳过扫描。"""
    target = natives_directory(mc_dir, version_id)
    target.mkdir(parents=True, exist_ok=True)
    if not native_jars:
        return target
    fp = _fingerprint(native_jars, excludes)
    if _manifest_matches(target, fp):
        return target
    extract_natives(native_jars, target, excludes)
    _write_manifest(target, fp)
    return target
