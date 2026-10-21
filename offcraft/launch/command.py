"""启动命令拼装：占位符展开、JVM/游戏参数、Java 选择。"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from offcraft import APP_NAME, __version__
from offcraft.launch.auth import OfflineProfile
from offcraft.launch.java import JavaCandidate, detect_java
from offcraft.launch.libraries import ClasspathPlan, build_classpath
from offcraft.launch.natives import ensure_natives, natives_directory
from offcraft.meta.rules import OsInfo, is_allowed
from offcraft.meta.version import MergedVersion

__all__ = ["CommandPlan", "LaunchError", "LaunchOptions", "build_command", "shell_quote"]

_PLACEHOLDER = re.compile(r"\$\{([^}]+)\}")


class LaunchError(RuntimeError):
    """启动参数无法生成。"""


@dataclass(slots=True)
class LaunchOptions:
    memory_mb: int = 4096
    width: int | None = None
    height: int | None = None
    fullscreen: bool = False
    java_path: str | None = None
    extra_jvm: list[str] = field(default_factory=list)
    extra_game: list[str] = field(default_factory=list)
    launcher_name: str = APP_NAME
    launcher_version: str = __version__

    def normalized_memory(self) -> int:
        return max(512, min(65536, int(self.memory_mb)))


@dataclass(slots=True)
class CommandPlan:
    argv: list[str]
    cwd: Path
    java: JavaCandidate
    natives_dir: Path
    classpath: ClasspathPlan
    warnings: list[str] = field(default_factory=list)

    @property
    def command_line(self) -> str:
        return " ".join(shell_quote(part) for part in self.argv)


def shell_quote(value: str) -> str:
    if value and not re.search(r'[\s"]', value):
        return value
    return '"' + value.replace('"', '\\"') + '"'


def _flatten(items: Iterable[Any], os_info: OsInfo, features: dict[str, bool]) -> list[str]:
    result: list[str] = []
    for item in items:
        if isinstance(item, str):
            result.append(item)
            continue
        if not isinstance(item, dict):
            continue
        if not is_allowed(item.get("rules"), os_info, features):
            continue
        value = item.get("value")
        if isinstance(value, str):
            result.append(value)
        elif isinstance(value, list):
            result.extend(str(v) for v in value)
    return result


def _expand(text: str, subs: dict[str, str], warnings: list[str]) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in subs:
            return subs[key]
        warnings.append(f"未知占位符：${{{key}}}")
        return ""

    return _PLACEHOLDER.sub(replace, text)


def _substitutions(
    version: MergedVersion,
    mc_dir: Path,
    profile: OfflineProfile,
    options: LaunchOptions,
    classpath: str,
    natives_dir: Path,
) -> dict[str, str]:
    separator = ";" if sys.platform == "win32" else ":"
    return {
        "auth_player_name": profile.name,
        "auth_uuid": profile.uuid,
        "auth_access_token": profile.access_token,
        "auth_session": profile.auth_session,
        "auth_xuid": profile.xuid,
        "clientid": "",
        "user_type": profile.user_type,
        "version_name": version.id,
        "version_type": version.type,
        "game_directory": str(mc_dir),
        "assets_root": str(mc_dir / "assets"),
        "assets_index_name": version.assets,
        "natives_directory": str(natives_dir),
        "classpath": classpath,
        "classpath_separator": separator,
        "library_directory": str(mc_dir / "libraries"),
        "launcher_name": options.launcher_name,
        "launcher_version": options.launcher_version,
        "resolution_width": str(options.width or 854),
        "resolution_height": str(options.height or 480),
        "quickPlayPath": "",
        "quickPlaySingleplayer": "",
        "quickPlayMultiplayer": "",
        "quickPlayRealms": "",
    }


def _collect_excludes(version: MergedVersion, os_info: OsInfo, features: dict[str, bool]) -> list[str]:
    excludes: list[str] = []
    for library in version.libraries:
        if not is_allowed(library.get("rules"), os_info, features):
            continue
        extract = library.get("extract") or {}
        for item in extract.get("exclude") or []:
            if item not in excludes:
                excludes.append(str(item))
    return excludes


def build_command(
    version: MergedVersion,
    mc_dir: Path | str,
    profile: OfflineProfile,
    *,
    options: LaunchOptions | None = None,
    os_info: OsInfo | None = None,
    extract_natives_now: bool = True,
) -> CommandPlan:
    mc_dir = Path(mc_dir)
    options = options or LaunchOptions()
    os_info = os_info or OsInfo()

    features: dict[str, bool] = {"is_demo_user": False, "has_custom_resolution": False}
    if options.width and options.height:
        features["has_custom_resolution"] = True

    classpath = build_classpath(version, mc_dir, os_info, features)
    if classpath.missing:
        preview = "\n".join(f"  - {path}" for path in classpath.missing[:10])
        raise LaunchError(
            f"缺少 {len(classpath.missing)} 个依赖文件，请先安装/修复该版本：\n{preview}"
        )

    natives_dir = natives_directory(mc_dir, version.id)
    excludes = _collect_excludes(version, os_info, features)
    if extract_natives_now:
        ensure_natives(version.id, mc_dir, classpath.native_jars, excludes)
    else:
        natives_dir.mkdir(parents=True, exist_ok=True)

    component = (version.raw.get("javaVersion") or {}).get("component")
    try:
        java = detect_java(
            mc_dir,
            version.java_major,
            override=options.java_path,
            preferred_component=component,
        )
    except Exception as exc:  # noqa: BLE001 - 转为启动错误
        raise LaunchError(str(exc)) from exc

    subs = _substitutions(version, mc_dir, profile, options, classpath.classpath, natives_dir)
    warnings: list[str] = list(classpath.warnings)

    jvm_items = _flatten((version.arguments or {}).get("jvm") or [], os_info, features)
    jvm_args = [f"-Xms{min(options.normalized_memory(), 1024)}M", f"-Xmx{options.normalized_memory()}M"]
    jvm_args.extend(_expand(item, subs, warnings) for item in jvm_items)

    if not any(item.startswith("-Djava.library.path=") for item in jvm_args):
        jvm_args.append(f"-Djava.library.path={natives_dir}")
    if "-cp" not in jvm_args and "-classpath" not in jvm_args:
        jvm_args.extend(["-cp", classpath.classpath])
    jvm_args.extend(options.extra_jvm)

    game_args: list[str] = []
    arguments = version.arguments or {}
    if arguments.get("game"):
        game_args.extend(
            _expand(item, subs, warnings)
            for item in _flatten(arguments["game"], os_info, features)
        )
    elif version.minecraft_arguments:
        game_args.extend(
            _expand(item, subs, warnings) for item in version.minecraft_arguments.split(" ")
        )
    else:
        warnings.append("版本既没有 arguments 也没有 minecraftArguments")

    if options.fullscreen and arguments.get("game"):
        game_args.append("--fullscreen")
    game_args.extend(options.extra_game)

    argv = [str(java.path), *jvm_args, version.main_class, *game_args]
    return CommandPlan(
        argv=argv,
        cwd=mc_dir,
        java=java,
        natives_dir=natives_dir,
        classpath=classpath,
        warnings=warnings,
    )
