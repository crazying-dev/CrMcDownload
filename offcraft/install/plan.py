"""根据版本 JSON 生成安装计划（纯函数，可 dry-run）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from offcraft.mirror import Mirror
from offcraft.meta.rules import OsInfo, is_allowed
from offcraft.meta.version import MergedVersion
from offcraft.net.downloader import DownloadTask

__all__ = [
    "InstallOptions",
    "InstallPlan",
    "InstallError",
    "add_asset_objects",
    "build_plan",
    "maven_path",
]

LIBS = "https://libraries.minecraft.net/"
RESOURCES = "https://resources.download.minecraft.net/"


class InstallError(RuntimeError):
    """安装计划无法生成。"""


@dataclass(frozen=True, slots=True)
class InstallOptions:
    include_assets: bool = True
    include_natives: bool = True

    @property
    def label(self) -> str:
        return "完整安装" if self.include_assets else "快速安装（跳过资源）"


@dataclass(slots=True)
class InstallPlan:
    mc_dir: Path
    version_id: str
    version_json: dict[str, Any]
    version: MergedVersion
    tasks: list[DownloadTask] = field(default_factory=list)
    client_jar: Path | None = None
    asset_index_id: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def version_json_path(self) -> Path:
        return self.mc_dir / "versions" / self.version_id / f"{self.version_id}.json"

    @property
    def total_bytes(self) -> int:
        return sum(task.size or 0 for task in self.tasks)

    def summary(self) -> str:
        return f"{self.version_id}: {len(self.tasks)} 个文件，约 {self.total_bytes / 1048576:.1f} MiB"


def maven_path(name: str) -> str:
    """Maven 坐标 → 仓库相对路径。支持 ``group:artifact:version[:classifier][@ext]``。"""
    ext = "jar"
    if "@" in name:
        name, ext = name.rsplit("@", 1)
    parts = name.split(":")
    if len(parts) < 3:
        raise InstallError(f"非法 Maven 坐标：{name}")
    group, artifact, version = parts[0], parts[1], parts[2]
    classifier = parts[3] if len(parts) > 3 else None
    filename = f"{artifact}-{version}"
    if classifier:
        filename += f"-{classifier}"
    filename += f".{ext}"
    return f"{group.replace('.', '/')}/{artifact}/{version}/{filename}"


def maven_path_with_classifier(name: str, classifier: str) -> str:
    parts = name.split(":")
    if len(parts) >= 4:
        parts = parts[:3] + [classifier]
    else:
        parts = parts + [classifier]
    return maven_path(":".join(parts))


def _artifact_url(library: dict[str, Any], path: str) -> str:
    artifact = (library.get("downloads") or {}).get("artifact") or {}
    if artifact.get("url"):
        return artifact["url"]
    base = library.get("url") or LIBS
    return base.rstrip("/") + "/" + path


def _add_library(
    library: dict[str, Any],
    plan: InstallPlan,
    mirror: Mirror,
    os_info: OsInfo,
    features: dict[str, bool],
    options: InstallOptions,
) -> None:
    if not is_allowed(library.get("rules"), os_info, features):
        return
    downloads = library.get("downloads") or {}
    artifact = downloads.get("artifact")
    if artifact and artifact.get("path"):
        path = artifact["path"]
    elif library.get("name"):
        path = maven_path(library["name"])
    else:
        plan.warnings.append(f"库缺少坐标，已跳过：{library!r}")
        return

    url = _artifact_url(library, path)
    plan.tasks.append(
        DownloadTask(
            url=mirror.rewrite(url),
            dest=plan.mc_dir / "libraries" / path,
            sha1=artifact.get("sha1") if artifact else None,
            size=artifact.get("size") if artifact else None,
            label=library.get("name", path),
        )
    )

    natives = library.get("natives") or {}
    classifier = natives.get("windows")
    if not classifier or not options.include_natives:
        return
    classifiers = downloads.get("classifiers") or {}
    native = classifiers.get(classifier)
    native_path = (native or {}).get("path") or maven_path_with_classifier(
        library["name"], classifier
    )
    native_url = (native or {}).get("url") or f"{LIBS.rstrip('/')}/{native_path}"
    plan.tasks.append(
        DownloadTask(
            url=mirror.rewrite(native_url),
            dest=plan.mc_dir / "libraries" / native_path,
            sha1=(native or {}).get("sha1"),
            size=(native or {}).get("size"),
            label=f"{library.get('name', path)}:{classifier}",
        )
    )


def _asset_tasks(plan: InstallPlan, index: dict[str, Any], mirror: Mirror) -> list[DownloadTask]:
    if index.get("virtual") or index.get("map_to_resources"):
        plan.warnings.append(
            f"资源索引 {plan.asset_index_id} 为旧版虚拟资源，暂不自动映射"
            "（游戏仍可启动，但缺少声音/语言）"
        )
        return []
    tasks: list[DownloadTask] = []
    for name, meta in (index.get("objects") or {}).items():
        digest = meta.get("hash")
        if not digest:
            continue
        prefix = digest[:2]
        tasks.append(
            DownloadTask(
                url=mirror.rewrite(f"{RESOURCES}{prefix}/{digest}"),
                dest=plan.mc_dir / "assets" / "objects" / prefix / digest,
                sha1=digest,
                size=meta.get("size"),
                label=name,
            )
        )
    return tasks


def build_plan(
    mc_dir: Path | str,
    version_id: str,
    version_json: dict[str, Any],
    merged: MergedVersion,
    mirror: Mirror,
    *,
    os_info: OsInfo | None = None,
    features: dict[str, bool] | None = None,
    options: InstallOptions | None = None,
) -> InstallPlan:
    """生成安装计划（不含资源 objects，需先下载索引后调用 :func:`add_asset_objects`）。"""
    mc_dir = Path(mc_dir)
    os_info = os_info or OsInfo()
    features = features or {}
    options = options or InstallOptions()

    plan = InstallPlan(
        mc_dir=mc_dir,
        version_id=version_id,
        version_json=version_json,
        version=merged,
    )

    client = (merged.downloads or {}).get("client") or {}
    jar_id = merged.jar_id
    jar_path = mc_dir / "versions" / jar_id / f"{jar_id}.jar"
    if client.get("url"):
        plan.client_jar = jar_path
        plan.tasks.append(
            DownloadTask(
                url=mirror.rewrite(client["url"]),
                dest=jar_path,
                sha1=client.get("sha1"),
                size=client.get("size"),
                label=f"{version_id} client.jar",
            )
        )
    else:
        plan.warnings.append(f"{version_id} 的 JSON 没有 client 下载信息")

    for library in merged.libraries:
        _add_library(library, plan, mirror, os_info, features, options)

    index = merged.asset_index or {}
    plan.asset_index_id = index.get("id", "")
    if index.get("url") and plan.asset_index_id:
        plan.tasks.append(
            DownloadTask(
                url=mirror.rewrite(index["url"]),
                dest=mc_dir / "assets" / "indexes" / f"{plan.asset_index_id}.json",
                sha1=index.get("sha1"),
                size=index.get("size"),
                label=f"assets index {plan.asset_index_id}",
            )
        )

    log_file = ((merged.logging_client or {}).get("file") or {})
    if log_file.get("url") and log_file.get("id"):
        plan.tasks.append(
            DownloadTask(
                url=mirror.rewrite(log_file["url"]),
                dest=mc_dir / "assets" / "log_configs" / log_file["id"],
                sha1=log_file.get("sha1"),
                size=log_file.get("size"),
                label=f"log config {log_file['id']}",
            )
        )

    return plan


def add_asset_objects(plan: InstallPlan, index_data: dict[str, Any], mirror: Mirror) -> None:
    """资源索引下载完成后，把 objects 追加进计划。"""
    plan.tasks.extend(_asset_tasks(plan, index_data, mirror))
