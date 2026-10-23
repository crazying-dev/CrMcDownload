"""本地实例发现与加载器识别。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "Instance",
    "detect_loader",
    "discover_instances",
    "instance_game_dir",
    "read_pack_metadata",
    "rename_instance",
    "safe_instance_id",
]

_VERSION_RE = re.compile(r"^\d+(?:\.\d+)+")

#: 目录名里不允许出现的字符（Windows）
_BAD_ID = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


@dataclass(frozen=True, slots=True)
class Instance:
    id: str
    loader: str
    mc_version: str
    type: str
    java_major: int | None
    size_bytes: int
    mtime: float
    broken: str | None = None

    @property
    def display_name(self) -> str:
        return self.id

    @property
    def label(self) -> str:
        return f"{self.loader} {self.mc_version}".strip()


def detect_loader(main_class: str, libraries: list[dict]) -> str:
    names = " ".join(str(lib.get("name", "")) for lib in libraries).lower()
    main = (main_class or "").lower()
    if "neoforge" in names:
        return "NeoForge"
    if "fabric" in main or "net.fabricmc" in names:
        return "Fabric"
    if "quilt" in main or "org.quiltmc" in names:
        return "Quilt"
    if "forge" in main or "net.minecraftforge" in names:
        return "Forge"
    return "原版"


def _mc_version(raw: dict, version_id: str) -> str:
    explicit = raw.get("clientVersion") or raw.get("inheritsFrom")
    if explicit:
        return str(explicit)
    match = _VERSION_RE.match(version_id)
    return match.group(0) if match else version_id


def instance_game_dir(mc_dir: Path | str, version_id: str, isolated: bool = True) -> Path:
    """版本的专属游戏目录（版本隔离时是 ``versions/<id>``）。"""
    mc_dir = Path(mc_dir)
    return mc_dir / "versions" / version_id if isolated else mc_dir


def read_pack_metadata(mc_dir: Path | str, version_id: str) -> dict | None:
    """读取整合包安装时写下的 ``offcraft-pack.json``。"""
    path = Path(mc_dir) / "versions" / version_id / "offcraft-pack.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def discover_instances(mc_dir: Path | str) -> list[Instance]:
    mc_dir = Path(mc_dir)
    versions_dir = mc_dir / "versions"
    result: list[Instance] = []
    if not versions_dir.is_dir():
        return result
    for folder in sorted(versions_dir.iterdir()):
        if not folder.is_dir():
            continue
        json_path = folder / f"{folder.name}.json"
        if not json_path.is_file():
            continue
        size = sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())
        try:
            raw = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            result.append(
                Instance(
                    id=folder.name,
                    loader="未知",
                    mc_version=folder.name,
                    type="unknown",
                    java_major=None,
                    size_bytes=size,
                    mtime=json_path.stat().st_mtime,
                    broken=f"JSON 损坏：{exc}",
                )
            )
            continue
        java = (raw.get("javaVersion") or {}).get("majorVersion")
        result.append(
            Instance(
                id=folder.name,
                loader=detect_loader(raw.get("mainClass", ""), raw.get("libraries") or []),
                mc_version=_mc_version(raw, folder.name),
                type=raw.get("type", "release"),
                java_major=int(java) if java else None,
                size_bytes=size,
                mtime=json_path.stat().st_mtime,
            )
        )
    result.sort(key=lambda item: item.mtime, reverse=True)
    return result


def safe_instance_id(name: str, fallback: str = "") -> str:
    """把用户输入变成可用的版本目录名（兼显示名）。"""
    cleaned = _BAD_ID.sub("_", str(name or "").strip()).strip(". ")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:48] or (fallback or "")


def _inheriting(mc_dir: Path, version_id: str) -> list[str]:
    """列出所有 ``inheritsFrom == version_id`` 的已装版本。"""
    versions = mc_dir / "versions"
    users: list[str] = []
    if not versions.is_dir():
        return users
    for folder in versions.iterdir():
        if not folder.is_dir() or folder.name == version_id:
            continue
        path = folder / f"{folder.name}.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(raw, dict) and str(raw.get("inheritsFrom") or "") == version_id:
            users.append(folder.name)
    return users


def rename_instance(mc_dir: Path | str, old_id: str, new_id: str) -> tuple[str, str]:
    """把 ``versions/<old_id>`` 改名成 ``new_id``（同时改 JSON 文件名与 ``id`` 字段）。

    返回 ``(最终 id, 提示)``；以下情况保留原名并给出原因：

    * 新名字为空 / 非法 / 与原名字相同；
    * 同名目录已存在；
    * 还有其他版本 ``inheritsFrom`` 它（改名会让那些版本失效，例如 Forge 依赖原版）。
    """
    mc_dir = Path(mc_dir)
    old_id = str(old_id or "").strip()
    new_id = safe_instance_id(new_id)
    if not old_id or not new_id or new_id == old_id:
        return old_id, ""
    old_dir = mc_dir / "versions" / old_id
    if not old_dir.is_dir():
        return old_id, f"找不到版本目录 {old_id}"
    new_dir = mc_dir / "versions" / new_id
    if new_dir.exists():
        return old_id, f"{new_id} 已存在，保留原名 {old_id}"
    users = _inheriting(mc_dir, old_id)
    if users:
        return old_id, f"{'、'.join(users[:3])} 还在用 {old_id}，保留原名"
    try:
        old_dir.rename(new_dir)
    except OSError as exc:
        return old_id, f"改名失败：{exc}"
    old_json = new_dir / f"{old_id}.json"
    new_json = new_dir / f"{new_id}.json"
    if old_json.is_file():
        try:
            raw = json.loads(old_json.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                raw["id"] = new_id
                new_json.write_text(
                    json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                old_json.unlink(missing_ok=True)
        except (OSError, ValueError):
            try:
                old_json.rename(new_json)
            except OSError:
                pass
    return new_id, ""
