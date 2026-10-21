"""离线包导出 / 导入：把某个版本启动所需的本地文件打成 zip。"""

from __future__ import annotations

import json
import os
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from offcraft.install.plan import InstallOptions, build_plan
from offcraft.meta.version import load_version
from offcraft.mirror import DirectSource

__all__ = [
    "PackError",
    "PackInfo",
    "export_pack",
    "import_pack",
    "inspect_pack",
]

FORMAT = 1
META_NAME = "offcraft-pack.json"
ProgressFn = Callable[[int, int, str], None]


class PackError(RuntimeError):
    """离线包导出/导入失败。"""


@dataclass(slots=True)
class PackInfo:
    version_id: str
    created_at: str
    files: int = 0
    bytes: int = 0
    warnings: list[str] = field(default_factory=list)


def _version_files(mc_dir: Path, version_id: str) -> tuple[list[Path], list[str]]:
    merged = load_version(mc_dir, version_id)
    raw = merged.raw
    plan = build_plan(
        mc_dir,
        version_id,
        raw,
        merged,
        DirectSource(),
        options=InstallOptions(include_assets=True),
    )
    warnings: list[str] = []
    files = [task.dest for task in plan.tasks]
    index_id = plan.asset_index_id
    index_path = mc_dir / "assets" / "indexes" / f"{index_id}.json"
    if index_id and index_path.is_file():
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            warnings.append(f"资源索引无法读取：{exc}")
            index = {}
        for meta in (index.get("objects") or {}).values():
            digest = meta.get("hash")
            if digest:
                files.append(mc_dir / "assets" / "objects" / digest[:2] / digest)
    elif index_id:
        warnings.append(f"缺少资源索引 {index_id}.json，离线包将不含资源")
    files.append(mc_dir / "versions" / version_id / f"{version_id}.json")
    return files, warnings


def export_pack(
    mc_dir: Path | str,
    version_id: str,
    out_zip: Path | str,
    *,
    progress: ProgressFn | None = None,
) -> PackInfo:
    mc_dir = Path(mc_dir)
    out_zip = Path(out_zip)
    files, warnings = _version_files(mc_dir, version_id)
    missing = [p for p in files if not p.is_file()]
    if missing:
        preview = "\n".join(str(p) for p in missing[:10])
        raise PackError(f"有 {len(missing)} 个文件缺失，无法导出：\n{preview}")

    total = sum(p.stat().st_size for p in files)
    info = PackInfo(version_id=version_id, created_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_zip.with_name(out_zip.name + ".tmp")
    done = 0
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        meta = {
            "format": FORMAT,
            "version_id": version_id,
            "created_at": info.created_at,
            "files": len(files),
            "bytes": total,
        }
        zf.writestr(META_NAME, json.dumps(meta, ensure_ascii=False, indent=2))
        for path in files:
            arcname = path.relative_to(mc_dir).as_posix()
            zf.write(path, arcname)
            done += path.stat().st_size
            if progress is not None:
                progress(done, total, arcname)
    os.replace(tmp, out_zip)
    info.files = len(files)
    info.bytes = total
    info.warnings = warnings
    return info


def inspect_pack(zip_path: Path | str) -> dict:
    with zipfile.ZipFile(zip_path) as zf:
        try:
            return json.loads(zf.read(META_NAME).decode("utf-8"))
        except KeyError as exc:
            raise PackError("不是 OffCraft 离线包（缺少元数据）") from exc


def _safe_member(name: str) -> bool:
    if name.startswith("/") or "\\" in name or ":" in name:
        return False
    parts = Path(name).parts
    return not any(part in ("..", "") for part in parts)


def import_pack(
    zip_path: Path | str,
    mc_dir: Path | str,
    *,
    progress: ProgressFn | None = None,
) -> PackInfo:
    zip_path = Path(zip_path)
    mc_dir = Path(mc_dir)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            meta = inspect_pack(zip_path)
            if int(meta.get("format", 0)) != FORMAT:
                raise PackError(f"不支持的离线包格式：{meta.get('format')}")
            members = [m for m in zf.infolist() if m.filename != META_NAME and not m.is_dir()]
            total = sum(m.file_size for m in members)
            done = 0
            mc_dir.mkdir(parents=True, exist_ok=True)
            for member in members:
                if not _safe_member(member.filename):
                    raise PackError(f"离线包包含非法路径：{member.filename}")
                target = mc_dir / Path(member.filename)
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, target.open("wb") as dst:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        dst.write(chunk)
                done += member.file_size
                if progress is not None:
                    progress(done, total, member.filename)
    except zipfile.BadZipFile as exc:
        raise PackError(f"不是有效的 zip：{zip_path}") from exc
    info = PackInfo(
        version_id=str(meta.get("version_id", "")),
        created_at=str(meta.get("created_at", "")),
        files=len(members),
        bytes=total,
    )
    return info
