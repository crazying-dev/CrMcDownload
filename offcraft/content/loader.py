"""加载器（Fabric / Forge / NeoForge）：列出可选版本并安装到 ``versions/<id>``。"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from offcraft.mirror import Mirror, UnknownUpstreamError
from offcraft.net.downloader import DownloadTask, Downloader, ProgressTracker

__all__ = [
    "LOADERS",
    "LOADER_CONFLICTS",
    "LOADER_ROWS",
    "LoaderBuild",
    "LoaderError",
    "install_loader",
    "list_loader_versions",
    "loader_note",
    "loader_support",
    "loader_title",
    "mc_sort_key",
    "supports_mods",
]

#: 安装版本时可选的加载器
LOADERS: list[tuple[str, str]] = [
    ("vanilla", "原版"),
    ("fabric", "Fabric"),
    ("forge", "Forge"),
    ("neoforge", "NeoForge"),
]

#: 安装页里的加载器行（顺序即展示顺序，第一个高的行是原版）
LOADER_ROWS: list[tuple[str, str, str]] = [
    ("fabric", "Fabric", "轻量、随版本更新最快；绝大多数客户端模组优先支持 Fabric。"),
    ("neoforge", "NeoForge", "Forge 的社区分支，1.20.2 及以后的新版本首选，API 与 Forge 同源。"),
    ("forge", "Forge", "老牌加载器，1.12.2 等旧版本与旧整合包生态最完整。"),
]

#: 互斥的加载器：同一实例 / 同一 MC 版本只能装其中一个
LOADER_CONFLICTS: dict[str, tuple[str, ...]] = {
    "forge": ("neoforge",),
    "neoforge": ("forge",),
}

#: 能装模组 / 光影的加载器
MOD_LOADERS = {"Fabric", "Forge", "NeoForge", "Quilt"}

FABRIC_LIST = "https://meta.fabricmc.net/v2/versions/loader/{mc}"
FABRIC_PROFILE = "https://meta.fabricmc.net/v2/versions/loader/{mc}/{loader}/profile/json"
FORGE_METADATA = "https://maven.minecraftforge.net/net/minecraftforge/forge/maven-metadata.xml"
FORGE_INSTALLER = (
    "https://maven.minecraftforge.net/net/minecraftforge/forge/{v}/forge-{v}-installer.jar"
)
NEOFORGE_METADATA = "https://maven.neoforged.net/releases/net/neoforged/neoforge/maven-metadata.xml"
NEOFORGE_INSTALLER = (
    "https://maven.neoforged.net/releases/net/neoforged/neoforge/{v}/neoforge-{v}-installer.jar"
)

_INSTALLER_TIMEOUT = 900.0


class LoaderError(RuntimeError):
    """加载器列表或安装失败。"""


def supports_mods(loader: str) -> bool:
    """该实例是否支持模组 / 光影（原版实例不支持）。"""
    return str(loader or "").strip() in MOD_LOADERS


def loader_title(loader: str) -> str:
    """``fabric`` → ``Fabric``；未知 key 原样返回。"""
    key = str(loader or "").strip().lower()
    return dict(LOADERS).get(key, str(loader or ""))


def mc_sort_key(mc_version: str) -> tuple[int, ...]:
    """把 ``1.20.2`` / ``26.3`` 这类版本号变成可比较的元组。"""
    return _version_key(mc_version)


def loader_support(
    loader: str, mc_version: str, *, version_type: str = "release"
) -> tuple[bool, str]:
    """静态兼容性判断：该加载器能不能装到这个 MC 版本上。

    只靠版本号规则（不联网），返回 ``(是否兼容, 不兼容原因)``。
    """
    key = str(loader or "").strip().lower()
    if key in ("", "vanilla"):
        return True, ""
    if version_type and version_type != "release":
        return False, "加载器只提供正式版，快照 / 远古版请装原版"
    version = mc_sort_key(mc_version)
    if key == "fabric":
        if version and version < (1, 14):
            return False, "Fabric 只支持 1.14 及以上版本"
        return True, ""
    if key == "neoforge":
        if version and version < (1, 20, 2):
            return False, "NeoForge 只支持 1.20.2 及以上版本"
        return True, ""
    if key == "forge":
        return True, ""
    return False, f"未知加载器：{loader}"


def loader_note(loader: str, mc_version: str) -> str:
    """展示用的中性提示（不阻止安装）。"""
    key = str(loader or "").strip().lower()
    version = mc_sort_key(mc_version)
    if key == "forge" and version and version >= (1, 20, 4):
        return "1.20.4 之后官方建议改用 NeoForge"
    if key == "fabric":
        return "需要 Fabric API 才能跑大多数模组"
    if key == "neoforge":
        return "与 Forge 不能同时安装"
    return ""


@dataclass(slots=True)
class LoaderBuild:
    loader: str
    version: str
    mc_version: str
    stable: bool = True
    note: str = ""

    @property
    def label(self) -> str:
        tag = "" if self.stable else "（测试）"
        return f"{self.loader} {self.version}{tag}"


def _rewrite(mirror: Mirror, url: str) -> str:
    try:
        return mirror.rewrite(url)
    except UnknownUpstreamError:
        return url


def _candidates(mirror: Mirror, url: str) -> list[str]:
    """镜像优先，失败再回退官方源（镜像可能尚未升级出对应路由）。"""
    mirrored = _rewrite(mirror, url)
    return [mirrored, url] if mirrored != url else [url]


def _fetch_json(mirror: Mirror, downloader: Downloader, url: str):
    errors: list[str] = []
    for candidate in _candidates(mirror, url):
        try:
            return downloader.fetch_json(candidate)
        except Exception as exc:  # noqa: BLE001 - 镜像不可用时回退
            errors.append(f"{candidate.split('/')[2]}: {exc}")
    raise LoaderError("获取失败：" + "；".join(errors))


def _parse_lenient(raw: bytes):
    """容错解析：镜像缓存可能返回重复拼接的 JSON，只取第一段。"""
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        raise LoaderError("响应为空")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            value, _end = json.JSONDecoder().raw_decode(text)
            return value
        except json.JSONDecodeError as exc:
            raise LoaderError(f"JSON 解析失败：{exc}") from exc


def _fetch_bytes(mirror: Mirror, downloader: Downloader, url: str) -> bytes:
    errors: list[str] = []
    for candidate in _candidates(mirror, url):
        try:
            return downloader.fetch_bytes(candidate)
        except Exception as exc:  # noqa: BLE001 - 镜像不可用时回退
            errors.append(f"{candidate.split('/')[2]}: {exc}")
    raise LoaderError("获取失败：" + "；".join(errors))


def _fabric_versions(mirror: Mirror, downloader: Downloader, mc_version: str) -> list[LoaderBuild]:
    data = _parse_lenient(_fetch_bytes(mirror, downloader, FABRIC_LIST.format(mc=mc_version)))
    if not isinstance(data, list):
        raise LoaderError("Fabric 版本列表格式错误")
    result: list[LoaderBuild] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        loader = entry.get("loader") or {}
        version = str(loader.get("version") or "")
        if not version:
            continue
        result.append(
            LoaderBuild(
                loader="fabric",
                version=version,
                mc_version=mc_version,
                stable=bool(loader.get("stable", True)),
            )
        )
    return result


def _maven_versions(
    mirror: Mirror, downloader: Downloader, url: str, prefix: str, loader: str, mc_version: str
) -> list[LoaderBuild]:
    raw = _fetch_bytes(mirror, downloader, url)
    try:
        root = ET.fromstring(raw.decode("utf-8", errors="replace"))
    except ET.ParseError as exc:
        raise LoaderError(f"读取 {loader} 版本列表失败：{exc}") from exc
    versions = [node.text.strip() for node in root.iter("version") if node.text]
    result = [
        LoaderBuild(loader=loader, version=value, mc_version=mc_version)
        for value in versions
        if value.startswith(prefix)
    ]
    return result


def _neoforge_prefix(mc_version: str) -> str:
    """1.20.4 → ``20.4.``；1.21 → ``21.0.``；1.21.1 → ``21.1.``。"""
    parts = [part for part in str(mc_version).split(".") if part]
    if not parts:
        return ""
    if parts[0] == "1":
        rest = parts[1:]
    else:
        rest = parts
    if not rest:
        return ""
    if len(rest) == 1:
        return f"{rest[0]}.0."
    return f"{rest[0]}.{rest[1]}."


def list_loader_versions(
    loader: str,
    mc_version: str,
    *,
    mirror: Mirror,
    downloader: Downloader,
    limit: int = 40,
) -> list[LoaderBuild]:
    """列出某个加载器对指定 MC 版本的可用版本（新→旧）。"""
    key = str(loader or "").lower()
    if key in ("", "vanilla"):
        return []
    if key == "fabric":
        builds = _fabric_versions(mirror, downloader, mc_version)
    elif key == "forge":
        builds = _maven_versions(
            mirror, downloader, FORGE_METADATA, f"{mc_version}-", "forge", mc_version
        )
        builds.sort(key=lambda item: _version_key(item.version.split("-", 1)[-1]), reverse=True)
    elif key == "neoforge":
        prefix = _neoforge_prefix(mc_version)
        if not prefix:
            return []
        builds = _maven_versions(
            mirror, downloader, NEOFORGE_METADATA, prefix, "neoforge", mc_version
        )
        builds.sort(key=lambda item: _version_key(item.version), reverse=True)
    else:
        raise LoaderError(f"不支持的加载器：{loader}")
    return builds[: max(1, limit)]


def _version_key(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in str(text).replace("-", ".").replace("_", ".").split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(0)
    return tuple(parts[:6])


def _pick_java(mc_dir: Path, override: str | Path | None = None) -> Path:
    if override:
        return Path(override)
    from offcraft.launch.java import list_java  # 延迟导入：本模块在 downloader 分支也能单独测试

    candidates = list_java(mc_dir)
    if not candidates:
        raise LoaderError("未检测到 Java，无法运行加载器安装器（请先在设置里添加 Java）")
    best = max(candidates, key=lambda item: item.major)
    return Path(best.path)


def _download(
    mirror: Mirror,
    downloader: Downloader,
    url: str,
    dest: Path,
    *,
    cancel=None,
    label: str = "",
) -> bool:
    """镜像优先下载单个文件，失败回退官方源；已存在且大小一致则跳过。"""
    if dest.is_file() and dest.stat().st_size > 0:
        return True
    for candidate in _candidates(mirror, url):
        report = downloader.run(
            [DownloadTask(url=candidate, dest=dest, label=label)],
            tracker=ProgressTracker(),
            cancel=cancel,
        )
        if report.ok and dest.is_file() and dest.stat().st_size > 0:
            return True
    return False


def _version_dirs(mc_dir: Path) -> set[str]:
    folder = mc_dir / "versions"
    if not folder.is_dir():
        return set()
    return {item.name for item in folder.iterdir() if item.is_dir()}


def _run_installer(
    mc_dir: Path,
    jar: Path,
    java: Path,
    *,
    cancel: threading.Event | None = None,
    phase: Callable[[str], None] | None = None,
) -> None:
    if phase:
        phase("运行加载器安装器（可能需要几分钟）")
    command = [str(java), "-jar", str(jar), "--installClient", str(mc_dir)]
    flags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        flags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    proc = subprocess.Popen(
        command,
        cwd=str(mc_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=flags,
    )
    deadline = time.monotonic() + _INSTALLER_TIMEOUT
    output: list[str] = []
    try:
        while proc.poll() is None:
            if cancel is not None and cancel.is_set():
                proc.terminate()
                raise LoaderError("已取消安装")
            if time.monotonic() > deadline:
                proc.terminate()
                raise LoaderError("安装器运行超时")
            time.sleep(0.25)
        stream = proc.stdout
        if stream is not None:
            tail = stream.read().decode("utf-8", errors="replace").strip().splitlines()
            output = tail[-8:]
    finally:
        if proc.poll() is None:
            proc.kill()
        if proc.stdout is not None:
            proc.stdout.close()
    if proc.returncode != 0:
        detail = " / ".join(output) if output else f"退出码 {proc.returncode}"
        raise LoaderError(f"安装器执行失败：{detail}")


def install_loader(
    mc_dir: Path | str,
    mc_version: str,
    loader: str,
    version: str,
    *,
    mirror: Mirror,
    downloader: Downloader,
    manifest=None,
    java: str | Path | None = None,
    cancel: threading.Event | None = None,
    phase: Callable[[str], None] | None = None,
) -> str:
    """安装加载器，返回可启动的版本 id。"""
    mc_dir = Path(mc_dir)
    key = str(loader or "").lower()
    if key == "vanilla":
        return mc_version

    if key == "fabric":
        if phase:
            phase("获取 Fabric profile")
        profile = _parse_lenient(
            _fetch_bytes(
                mirror, downloader, FABRIC_PROFILE.format(mc=mc_version, loader=version)
            )
        )
        if not isinstance(profile, dict) or not profile.get("mainClass"):
            raise LoaderError("Fabric profile 格式错误")
        version_id = str(profile.get("id") or f"fabric-loader-{version}-{mc_version}")
        target = mc_dir / "versions" / version_id
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{version_id}.json").write_text(
            json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if manifest is not None:
            from offcraft.install.installer import install, prepare_install
            from offcraft.install.plan import InstallOptions

            if phase:
                phase("下载 Fabric 依赖库")
            plan = prepare_install(
                mc_dir,
                version_id,
                manifest,
                mirror,
                downloader,
                options=InstallOptions(include_assets=False),
            )
            result = install(plan, downloader, tracker=ProgressTracker(), cancel=cancel)
            if not result.ok:
                first = result.report.failed[0] if result.report.failed else (None, "未知错误")
                raise LoaderError(f"Fabric 依赖下载失败：{first[1]}")
        return version_id

    if key in ("forge", "neoforge"):
        if key == "forge":
            url = FORGE_INSTALLER.format(v=version)
            name = f"forge-{version}-installer.jar"
        else:
            url = NEOFORGE_INSTALLER.format(v=version)
            name = f"neoforge-{version}-installer.jar"
        cache_dir = mc_dir / "cache" / "installers"
        cache_dir.mkdir(parents=True, exist_ok=True)
        jar = cache_dir / name
        if phase:
            phase(f"下载 {loader} 安装器")
        if not _download(
            mirror, downloader, url, jar, cancel=cancel, label=name
        ):
            raise LoaderError(f"{loader} 安装器下载失败")
        java_path = _pick_java(mc_dir, java)
        before = _version_dirs(mc_dir)
        _run_installer(mc_dir, jar, java_path, cancel=cancel, phase=phase)
        created = sorted(_version_dirs(mc_dir) - before)
        if not created:
            raise LoaderError(f"{loader} 安装器没有生成新的版本目录")
        created.sort(key=lambda item: (item == mc_version, _version_key(item)), reverse=True)
        return created[0]

    raise LoaderError(f"不支持的加载器：{loader}")
