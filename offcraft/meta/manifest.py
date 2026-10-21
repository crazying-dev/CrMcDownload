"""版本清单（version_manifest_v2.json）的解析与过滤。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

__all__ = ["VersionManifest", "VersionSummary"]


@dataclass(frozen=True, slots=True)
class VersionSummary:
    id: str
    type: str
    url: str
    time: str = ""
    release_time: str = ""
    sha1: str = ""
    compliance_level: int = 1

    @property
    def label(self) -> str:
        return {
            "release": "正式版",
            "snapshot": "快照",
            "old_beta": "远古 Beta",
            "old_alpha": "远古 Alpha",
        }.get(self.type, self.type)


@dataclass(frozen=True, slots=True)
class VersionManifest:
    latest_release: str
    latest_snapshot: str
    versions: tuple[VersionSummary, ...]

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "VersionManifest":
        latest = data.get("latest") or {}
        versions = tuple(
            VersionSummary(
                id=item["id"],
                type=item.get("type", "release"),
                url=item.get("url", ""),
                time=item.get("time", ""),
                release_time=item.get("releaseTime", ""),
                sha1=item.get("sha1", ""),
                compliance_level=int(item.get("complianceLevel", 1)),
            )
            for item in data.get("versions", [])
            if item.get("id") and item.get("url")
        )
        return cls(
            latest_release=latest.get("release", ""),
            latest_snapshot=latest.get("snapshot", ""),
            versions=versions,
        )

    def find(self, version_id: str) -> VersionSummary | None:
        for item in self.versions:
            if item.id == version_id:
                return item
        return None

    def filter(self, kinds: Iterable[str] | None = None, query: str = "") -> list[VersionSummary]:
        allowed = set(kinds) if kinds else None
        text = query.strip().lower()
        result = []
        for item in self.versions:
            if allowed is not None and item.type not in allowed:
                continue
            if text and text not in item.id.lower():
                continue
            result.append(item)
        return result

    def __len__(self) -> int:  # pragma: no cover - 便利方法
        return len(self.versions)
