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

#: 单个文件解压上限（512 MiB）
MAX_FILE_BYTES = 512 * 1024 * 1024
#: 一次导入的解压总量上限（4 GiB）
MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
#: 成员数量上限，防止「十万个小文件」把磁盘 / inode 吃光
MAX_MEMBERS = 20000
#: 元数据文件大小上限
MAX_META_BYTES = 8 * 1024 * 1024

#: 覆盖策略：已存在就报错 / 跳过 / 覆盖
CONFLICT_ERROR = "error"
CONFLICT_SKIP = "skip"
CONFLICT_OVERWRITE = "overwrite"
_CONFLICTS = (CONFLICT_ERROR, CONFLICT_SKIP, CONFLICT_OVERWRITE)


class PackError(RuntimeError):
    """离线包导出/导入失败。"""


@dataclass(slots=True)
class PackInfo:
    version_id: str
    created_at: str
    files: int = 0
    bytes: int = 0
    skipped: int = 0
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


def inspect_pack(zip_path: Path | str, *, max_bytes: int = MAX_META_BYTES) -> dict:
    """读取离线包元数据；读取前先检查大小，防止被超大 JSON 拖死。"""
    with zipfile.ZipFile(zip_path) as zf:
        try:
            entry = zf.getinfo(META_NAME)
        except KeyError as exc:
            raise PackError("不是 OffCraft 离线包（缺少元数据）") from exc
        if entry.file_size > max_bytes:
            raise PackError(f"离线包元数据异常（{entry.file_size} 字节），已拒绝导入")
        try:
            data = json.loads(zf.read(META_NAME).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PackError(f"离线包元数据不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise PackError("离线包元数据格式错误")
    return data


def _safe_member(name: str) -> bool:
    if name.startswith("/") or "\\" in name or ":" in name:
        return False
    parts = Path(name).parts
    return not any(part in ("..", "") for part in parts)


def _target_of(mc_dir: Path, name: str) -> Path:
    """成员名 → 绝对路径；跑出游戏目录就报错（与 _safe_member 双重保险）。"""
    root = mc_dir.resolve()
    target = (root / Path(name)).resolve()
    if root != target and root not in target.parents:
        raise PackError(f"离线包包含非法路径：{name}")
    return target


def _plan_members(
    mc_dir: Path,
    members: list[zipfile.ZipInfo],
    *,
    on_conflict: str,
    max_total_bytes: int,
    max_file_bytes: int,
    max_members: int,
) -> tuple[list[tuple[zipfile.ZipInfo, Path, str]], int]:
    """在动磁盘之前把所有成员检查完：路径、成员数、单文件与总体积、重名与冲突。"""
    if len(members) > max_members:
        raise PackError(f"离线包成员过多（{len(members)} > {max_members}），已拒绝导入")
    planned: list[tuple[zipfile.ZipInfo, Path, str]] = []
    total = 0
    seen: set[str] = set()
    for member in members:
        name = member.filename
        if not _safe_member(name):
            raise PackError(f"离线包包含非法路径：{name}")
        if member.file_size > max_file_bytes:
            raise PackError(
                f"{name} 超过单文件上限（{member.file_size} > {max_file_bytes} 字节）"
            )
        total += member.file_size
        if total > max_total_bytes:
            raise PackError(f"离线包解压后超过总体积上限（{max_total_bytes} 字节），已拒绝")
        target = _target_of(mc_dir, name)
        key = str(target).lower()  # Windows 文件系统不区分大小写
        if key in seen:
            raise PackError(f"离线包包含重复成员：{name}")
        seen.add(key)
        exists = target.exists()
        if exists and on_conflict == CONFLICT_ERROR:
            raise PackError(
                f"目标文件已存在：{target}\n可改用覆盖（on_conflict='overwrite'）或跳过（'skip'）"
            )
        if exists and on_conflict == CONFLICT_SKIP:
            planned.append((member, target, "skip"))
        elif exists:
            planned.append((member, target, "overwrite"))
        else:
            planned.append((member, target, "write"))
    return planned, total


def import_pack(
    zip_path: Path | str,
    mc_dir: Path | str,
    *,
    progress: ProgressFn | None = None,
    on_conflict: str = CONFLICT_ERROR,
    max_total_bytes: int = MAX_TOTAL_BYTES,
    max_file_bytes: int = MAX_FILE_BYTES,
    max_members: int = MAX_MEMBERS,
) -> PackInfo:
    """把离线包解压到游戏目录。

    * 写入前完成全部校验（路径越界、成员数、单文件/总体积、重名、冲突），
      任何一项不合规都在动磁盘之前抛 :class:`PackError`；
    * 单个文件先写 ``.importing`` 临时文件再原子替换，失败不会留下半截文件；
    * 中途失败会删除本次新建的文件（覆盖策略下已被替换的文件无法回滚）；
    * ``on_conflict`` 三选一：``error``（默认）/ ``skip`` / ``overwrite``。
    """
    if on_conflict not in _CONFLICTS:
        raise PackError(f"未知的覆盖策略：{on_conflict}")
    zip_path = Path(zip_path)
    mc_dir = Path(mc_dir)
    if not zip_path.is_file():
        raise PackError(f"找不到离线包：{zip_path}")
    skipped = 0
    try:
        with zipfile.ZipFile(zip_path) as zf:
            meta = inspect_pack(zip_path)
            if int(meta.get("format", 0)) != FORMAT:
                raise PackError(f"不支持的离线包格式：{meta.get('format')}")
            members = [m for m in zf.infolist() if m.filename != META_NAME and not m.is_dir()]
            planned, total = _plan_members(
                mc_dir,
                members,
                on_conflict=on_conflict,
                max_total_bytes=max_total_bytes,
                max_file_bytes=max_file_bytes,
                max_members=max_members,
            )
            mc_dir.mkdir(parents=True, exist_ok=True)
            done = 0
            written_total = 0
            created: list[Path] = []
            try:
                for member, target, action in planned:
                    if action == "skip":
                        skipped += 1
                        done += member.file_size
                        if progress is not None:
                            progress(done, total, member.filename)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    tmp = target.with_name(target.name + ".importing")
                    written = 0
                    try:
                        with zf.open(member) as src, tmp.open("wb") as dst:
                            while True:
                                chunk = src.read(1024 * 1024)
                                if not chunk:
                                    break
                                written += len(chunk)
                                written_total += len(chunk)
                                if written > max_file_bytes:
                                    raise PackError(
                                        f"{member.filename} 实际大小超出单文件上限，已中止"
                                    )
                                if written_total > max_total_bytes:
                                    raise PackError("解压超出总体积上限，已中止")
                                dst.write(chunk)
                        os.replace(tmp, target)
                    finally:
                        if tmp.exists():
                            try:
                                tmp.unlink()
                            except OSError:
                                pass
                    if action == "write":
                        created.append(target)
                    done += member.file_size
                    if progress is not None:
                        progress(done, total, member.filename)
            except BaseException:
                # 失败回滚：只删本次新建的文件
                for path in created:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
                raise
    except zipfile.BadZipFile as exc:
        raise PackError(f"不是有效的 zip：{zip_path}") from exc
    return PackInfo(
        version_id=str(meta.get("version_id", "")),
        created_at=str(meta.get("created_at", "")),
        files=len(members),
        bytes=total,
        skipped=skipped,
    )
