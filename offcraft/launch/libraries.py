"""根据版本 JSON 计算 classpath 与 natives 列表。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from offcraft.install.plan import maven_path, maven_path_with_classifier
from offcraft.meta.rules import OsInfo, is_allowed
from offcraft.meta.version import MergedVersion

__all__ = ["ClasspathPlan", "build_classpath"]


@dataclass(slots=True)
class ClasspathPlan:
    entries: list[Path] = field(default_factory=list)
    native_jars: list[Path] = field(default_factory=list)
    missing: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def classpath(self) -> str:
        return ";".join(str(path) for path in self.entries)

    @property
    def ok(self) -> bool:
        return not self.missing


def _artifact_path(library: dict[str, Any]) -> str | None:
    artifact = (library.get("downloads") or {}).get("artifact") or {}
    if artifact.get("path"):
        return artifact["path"]
    name = library.get("name")
    if name:
        try:
            return maven_path(name)
        except Exception:  # noqa: BLE001 - 非法坐标
            return None
    return None


def _native_path(library: dict[str, Any], classifier: str) -> str | None:
    downloads = library.get("downloads") or {}
    native = (downloads.get("classifiers") or {}).get(classifier)
    if native and native.get("path"):
        return native["path"]
    name = library.get("name")
    if name:
        try:
            return maven_path_with_classifier(name, classifier)
        except Exception:  # noqa: BLE001
            return None
    return None


def build_classpath(
    version: MergedVersion,
    mc_dir: Path | str,
    os_info: OsInfo | None = None,
    features: dict[str, bool] | None = None,
) -> ClasspathPlan:
    mc_dir = Path(mc_dir)
    os_info = os_info or OsInfo()
    features = features or {}
    plan = ClasspathPlan()
    libraries_dir = mc_dir / "libraries"

    for library in version.libraries:
        if not is_allowed(library.get("rules"), os_info, features):
            continue

        relative = _artifact_path(library)
        if relative is None:
            plan.warnings.append(f"无法确定库路径，已跳过：{library.get('name')!r}")
        else:
            path = libraries_dir / relative
            plan.entries.append(path)
            if not path.is_file():
                plan.missing.append(path)

        classifier = (library.get("natives") or {}).get("windows")
        if classifier:
            native_relative = _native_path(library, classifier)
            if native_relative is None:
                plan.warnings.append(
                    f"无法确定 natives 路径：{library.get('name')!r} ({classifier})"
                )
                continue
            native_path = libraries_dir / native_relative
            plan.native_jars.append(native_path)
            if not native_path.is_file():
                plan.missing.append(native_path)

    jar_id = version.jar_id
    jar_path = mc_dir / "versions" / jar_id / f"{jar_id}.jar"
    plan.entries.append(jar_path)
    if not jar_path.is_file():
        plan.missing.append(jar_path)

    return plan
