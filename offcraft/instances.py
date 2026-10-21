"""本地实例发现与加载器识别。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

__all__ = ["Instance", "detect_loader", "discover_instances"]

_VERSION_RE = re.compile(r"^\d+(?:\.\d+)+")


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
