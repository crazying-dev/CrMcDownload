"""Modrinth 整合包（.mrpack）解析与安装。

安装流程：基础版本 → 加载器（Fabric 自动安装）→ 清单文件 → overrides。
整合包总是安装到独立的版本目录，避免与共享目录里的 mod 相互污染。
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from offcraft.content.install import ContentInstallError, download_file
from offcraft.install.installer import InstallOptions, install, prepare_install
from offcraft.install.plan import build_plan
from offcraft.meta.manifest import VersionManifest
from offcraft.meta.version import load_version
from offcraft.mirror import Mirror, UnknownUpstreamError
from offcraft.net.downloader import Downloader, ProgressTracker

__all__ = [
    "ModpackError",
    "ModpackInfo",
    "ModpackResult",
    "PackFile",
    "install_modpack",
    "read_mrpack",
    "sanitize_instance_name",
    "unique_instance_name",
]

PhaseHook = Callable[[str], None]

_INVALID_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_RESERVED = {
    "con", "prn", "aux", "nul",
    "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
    "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
}
_INDEX_NAME = "modrinth.index.json"
_OVERRIDE_PREFIXES = ("overrides/", "client-overrides/")
_MAX_ARCHIVE_BYTES = 4 * 1024 * 1024 * 1024


class ModpackError(RuntimeError):
    """整合包无法解析或安装。"""


@dataclass(frozen=True, slots=True)
class PackFile:
    path: str
    url: str
    sha1: str = ""
    sha512: str = ""
    size: int = 0
    env_client: str = ""

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "PackFile":
        hashes = data.get("hashes") or {}
        downloads = data.get("downloads") or []
        url = str(downloads[0]) if downloads else ""
        return cls(
            path=_safe_pack_path(str(data.get("path") or "")),
            url=url,
            sha1=str(hashes.get("sha1") or ""),
            sha512=str(hashes.get("sha512") or ""),
            size=int(data.get("fileSize") or 0),
            env_client=str((data.get("env") or {}).get("client") or ""),
        )


@dataclass(frozen=True, slots=True)
class ModpackInfo:
    name: str
    version_id: str
    summary: str
    mc_version: str
    loader: str
    loader_version: str
    files: tuple[PackFile, ...]
    format_version: int = 1

    @property
    def loader_label(self) -> str:
        if not self.loader:
            return "原版"
        return {"fabric": "Fabric", "forge": "Forge", "neoforge": "NeoForge", "quilt": "Quilt"}.get(
            self.loader, self.loader.title()
        )


def _safe_pack_path(path: str) -> str:
    text = (path or "").replace("\\", "/").strip()
    if not text:
        raise ModpackError("整合包文件路径为空")
    pure = PurePosixPath(text)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        raise ModpackError(f"整合包包含非法路径：{path}")
    if "\x00" in text or ":" in pure.parts[0]:
        raise ModpackError(f"整合包包含非法路径：{path}")
    return str(pure)


def read_mrpack(archive: Path | str) -> ModpackInfo:
    """读取 ``.mrpack`` 的 ``modrinth.index.json``。"""
    archive = Path(archive)
    if not archive.is_file():
        raise ModpackError(f"找不到整合包文件：{archive}")
    try:
        with zipfile.ZipFile(archive) as zf:
            if _INDEX_NAME not in zf.namelist():
                raise ModpackError("不是有效的 Modrinth 整合包（缺少 modrinth.index.json）")
            raw = json.loads(zf.read(_INDEX_NAME).decode("utf-8"))
    except ModpackError:
        raise
    except (OSError, zipfile.BadZipFile, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModpackError(f"整合包无法读取：{exc}") from exc
    if not isinstance(raw, dict):
        raise ModpackError("modrinth.index.json 格式错误")

    deps = raw.get("dependencies") or {}
    mc_version = str(deps.get("minecraft") or "")
    if not mc_version:
        raise ModpackError("整合包没有声明 Minecraft 版本")
    loader = ""
    loader_version = ""
    for key in ("fabric-loader", "quilt-loader", "neoforge", "forge"):
        if deps.get(key):
            loader = key.replace("-loader", "")
            loader_version = str(deps[key])
            break

    files: list[PackFile] = []
    for item in raw.get("files") or []:
        if not isinstance(item, dict) or not item.get("downloads"):
            continue
        files.append(PackFile.from_json(item))

    return ModpackInfo(
        name=str(raw.get("name") or archive.stem),
        version_id=str(raw.get("versionId") or "1.0.0"),
        summary=str(raw.get("summary") or ""),
        mc_version=mc_version,
        loader=loader,
        loader_version=loader_version,
        files=tuple(files),
        format_version=int(raw.get("formatVersion") or 1),
    )


def sanitize_instance_name(name: str, fallback: str = "modpack") -> str:
    cleaned = _INVALID_NAME.sub("_", (name or "").strip()).strip(". ")
    if not cleaned:
        cleaned = fallback
    if cleaned.lower() in _RESERVED:
        cleaned = f"{cleaned}-pack"
    return cleaned[:48]


def unique_instance_name(mc_dir: Path | str, base: str) -> str:
    versions = Path(mc_dir) / "versions"
    candidate = sanitize_instance_name(base)
    if not (versions / candidate).exists():
        return candidate
    for index in range(2, 100):
        name = f"{candidate}-{index}"
        if not (versions / name).exists():
            return name
    raise ModpackError(f"无法为新实例取名：{base}")


@dataclass(slots=True)
class ModpackResult:
    instance_id: str
    game_dir: Path
    installed: int = 0
    total: int = 0
    warnings: list[str] = field(default_factory=list)


def _version_ready(mc_dir: Path, version_id: str) -> bool:
    folder = mc_dir / "versions" / version_id
    return (folder / f"{version_id}.json").is_file() and any(folder.glob("*.jar"))


def _write_version_json(mc_dir: Path, version_id: str, payload: dict[str, Any]) -> Path:
    folder = mc_dir / "versions" / version_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{version_id}.json"
    payload = dict(payload)
    payload["id"] = version_id
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def _install_fabric(
    mc_dir: Path,
    instance_id: str,
    mc_version: str,
    loader_version: str,
    mirror: Mirror,
    downloader: Downloader,
    tracker: ProgressTracker,
    cancel: threading.Event | None,
) -> None:
    url = (
        "https://meta.fabricmc.net/v2/versions/loader/"
        f"{mc_version}/{loader_version}/profile/json"
    )
    try:
        profile = downloader.fetch_json(mirror.rewrite(url))
    except Exception as exc:  # noqa: BLE001
        raise ModpackError(f"Fabric 加载器信息获取失败：{exc}") from exc
    if not isinstance(profile, dict) or not profile.get("mainClass"):
        raise ModpackError("Fabric 加载器 profile 格式错误")
    profile["inheritsFrom"] = mc_version
    profile["id"] = instance_id
    _write_version_json(mc_dir, instance_id, profile)
    merged = load_version(mc_dir, instance_id)
    plan = build_plan(
        mc_dir,
        instance_id,
        profile,
        merged,
        mirror,
        options=InstallOptions(include_assets=False),
    )
    result = install(plan, downloader, tracker=tracker, cancel=cancel)
    if not result.ok:
        first = result.report.failed[0] if result.report.failed else (None, "未知错误")
        raise ModpackError(f"Fabric 加载器安装失败：{first[1]}")


def install_modpack(
    mc_dir: Path | str,
    archive: Path | str,
    *,
    mirror: Mirror,
    downloader: Downloader,
    manifest: VersionManifest | None = None,
    instance_name: str = "",
    isolated: bool = True,
    cancel: threading.Event | None = None,
    phase: PhaseHook | None = None,
) -> ModpackResult:
    """安装整合包到 ``versions/<instance>`` 并注册为可启动实例。"""
    mc_dir = Path(mc_dir)
    info = read_mrpack(archive)
    instance_id = sanitize_instance_name(instance_name or info.name)
    if (mc_dir / "versions" / instance_id / f"{instance_id}.json").is_file():
        raise ModpackError(f"实例 {instance_id} 已存在，请换一个名称")

    def say(text: str) -> None:
        if phase is not None:
            phase(text)

    tracker = ProgressTracker()
    warnings: list[str] = []
    installed = 0

    # 1) 基础版本
    if not _version_ready(mc_dir, info.mc_version):
        if manifest is None:
            raise ModpackError("缺少版本清单，无法自动安装基础版本")
        say(f"安装基础版本 {info.mc_version}")
        plan = prepare_install(
            mc_dir,
            info.mc_version,
            manifest,
            mirror,
            downloader,
            options=InstallOptions(include_assets=True),
        )
        result = install(plan, downloader, tracker=tracker, cancel=cancel)
        if not result.ok:
            first = result.report.failed[0] if result.report.failed else (None, "未知错误")
            raise ModpackError(f"基础版本安装失败：{first[1]}")

    # 2) 加载器
    if info.loader == "fabric":
        say(f"安装 Fabric {info.loader_version}")
        _install_fabric(
            mc_dir,
            instance_id,
            info.mc_version,
            info.loader_version,
            mirror,
            downloader,
            tracker,
            cancel,
        )
    else:
        if info.loader in ("forge", "neoforge", "quilt"):
            warnings.append(
                f"该整合包使用 {info.loader_label} 加载器，自动安装尚未支持：mods 已装好，"
                "请先用其它启动器安装对应加载器，或改用 Fabric 整合包。"
            )
        _write_version_json(
            mc_dir,
            instance_id,
            {
                "id": instance_id,
                "inheritsFrom": info.mc_version,
                "type": "release",
                "time": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
                "releaseTime": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
            },
        )

    # 3) 清单文件与 overrides
    game_dir = mc_dir / "versions" / instance_id if isolated else mc_dir
    game_dir.mkdir(parents=True, exist_ok=True)
    say("下载整合包文件")
    total = len(info.files)
    for item in info.files:
        if item.env_client == "unsupported":
            continue
        try:
            url = mirror.rewrite(item.url)
        except UnknownUpstreamError:
            url = item.url
            if "外部下载源" not in " ".join(warnings):
                warnings.append("部分文件来自外部下载源，将直连下载")
        try:
            download_file(
                downloader,
                url,
                game_dir / item.path,
                sha1=item.sha1,
                sha512=item.sha512,
                size=item.size or None,
                cancel=cancel,
            )
            installed += 1
        except ContentInstallError as exc:
            warnings.append(str(exc))

    say("释放整合包配置")
    _extract_overrides(archive, game_dir)

    metadata = {
        "schema": 1,
        "name": info.name,
        "version": info.version_id,
        "summary": info.summary,
        "minecraft": info.mc_version,
        "loader": info.loader,
        "loader_version": info.loader_version,
        "installed_at": int(time.time()),
    }
    (game_dir / "offcraft-pack.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return ModpackResult(
        instance_id=instance_id,
        game_dir=game_dir,
        installed=installed,
        total=total,
        warnings=warnings,
    )


def _extract_overrides(archive: Path, game_dir: Path) -> None:
    try:
        with zipfile.ZipFile(archive) as zf:
            total = 0
            for entry in zf.infolist():
                name = entry.filename.replace("\\", "/")
                prefix = next((p for p in _OVERRIDE_PREFIXES if name.startswith(p)), None)
                if prefix is None or entry.is_dir():
                    continue
                relative = name[len(prefix):]
                if not relative:
                    continue
                try:
                    safe = _safe_pack_path(relative)
                except ModpackError:
                    continue
                total += entry.file_size
                if total > _MAX_ARCHIVE_BYTES:
                    raise ModpackError("overrides 体积异常，已中止解压")
                dest = game_dir / safe
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(entry) as src, dest.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
    except zipfile.BadZipFile as exc:
        raise ModpackError(f"overrides 解压失败：{exc}") from exc
