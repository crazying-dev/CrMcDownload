"""执行安装计划：下载、校验、落盘、写版本 JSON。"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from offcraft.install.plan import (
    InstallError,
    InstallOptions,
    InstallPlan,
    add_asset_objects,
    build_plan,
)
from offcraft.meta.manifest import VersionManifest
from offcraft.meta.version import MergedVersion, VersionError, load_version, load_version_file
from offcraft.mirror import Mirror
from offcraft.net.downloader import (
    Cancelled,
    Downloader,
    DownloadReport,
    ProgressTracker,
)

__all__ = [
    "InstallError",
    "InstallOptions",
    "InstallPlan",
    "InstallResult",
    "install",
    "prepare_install",
]

ProgressHook = Callable[[ProgressTracker], None]


@dataclass(slots=True)
class InstallResult:
    plan: InstallPlan
    report: DownloadReport
    duration: float = 0.0
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.report.ok

    @property
    def installed_files(self) -> int:
        return len(self.report.succeeded)


def prepare_install(
    mc_dir: Path | str,
    version_id: str,
    manifest: VersionManifest,
    mirror: Mirror,
    downloader: Downloader,
    *,
    options: InstallOptions | None = None,
) -> InstallPlan:
    """拉取版本 JSON（必要时读取本地父版本）并生成完整安装计划。"""
    mc_dir = Path(mc_dir)
    options = options or InstallOptions()
    summary = manifest.find(version_id)
    if summary is None:
        raise InstallError(f"版本清单中不存在 {version_id}")

    raw = downloader.fetch_json(mirror.rewrite(summary.url))
    if not isinstance(raw, dict):
        raise InstallError(f"{version_id} 的版本 JSON 格式错误")
    raw.setdefault("id", version_id)

    parent = raw.get("inheritsFrom")
    if parent:
        try:
            merged = load_version(mc_dir, version_id)
        except VersionError as exc:
            raise InstallError(
                f"{version_id} 依赖父版本 {parent}，请先安装父版本（{exc}）"
            ) from exc
    else:
        merged = MergedVersion(id=version_id, raw=raw, chain=(version_id,))

    plan = build_plan(mc_dir, version_id, raw, merged, mirror, options=options)

    if options.include_assets and plan.asset_index_id:
        index = merged.asset_index or {}
        if index.get("url"):
            try:
                index_data = downloader.fetch_json(mirror.rewrite(index["url"]))
                add_asset_objects(plan, index_data, mirror)
            except Exception as exc:  # noqa: BLE001 - 资源索引失败不阻塞启动
                plan.warnings.append(f"资源索引获取失败，将跳过资源下载：{exc}")
    return plan


def install(
    plan: InstallPlan,
    downloader: Downloader,
    *,
    tracker: ProgressTracker | None = None,
    cancel: threading.Event | None = None,
    progress: ProgressHook | None = None,
) -> InstallResult:
    """执行计划；返回下载报告，成功后写出 ``versions/<id>/<id>.json``。"""
    started = time.monotonic()
    tracker = tracker or ProgressTracker()
    report = downloader.run(plan.tasks, tracker=tracker, cancel=cancel)
    if not report.ok:
        return InstallResult(plan=plan, report=report, duration=time.monotonic() - started)

    if plan.client_jar is not None and not plan.client_jar.is_file():
        raise InstallError(f"客户端 jar 缺失：{plan.client_jar}")

    version_dir = plan.mc_dir / "versions" / plan.version_id
    version_dir.mkdir(parents=True, exist_ok=True)
    payload = dict(plan.version_json)
    payload["id"] = plan.version_id
    tmp = plan.version_json_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, plan.version_json_path)

    return InstallResult(
        plan=plan,
        report=report,
        duration=time.monotonic() - started,
        warnings=list(plan.warnings),
    )


def install_version(
    mc_dir: Path | str,
    version_id: str,
    manifest: VersionManifest,
    mirror: Mirror,
    downloader: Downloader,
    *,
    options: InstallOptions | None = None,
    tracker: ProgressTracker | None = None,
    cancel: threading.Event | None = None,
    progress: ProgressHook | None = None,
) -> InstallResult:
    """``prepare_install`` + ``install`` 的便捷组合。"""
    plan = prepare_install(
        mc_dir, version_id, manifest, mirror, downloader, options=options
    )
    return install(plan, downloader, tracker=tracker, cancel=cancel, progress=progress)


def load_local_version_json(mc_dir: Path | str, version_id: str) -> dict[str, Any]:
    return load_version_file(Path(mc_dir) / "versions" / version_id / f"{version_id}.json")
